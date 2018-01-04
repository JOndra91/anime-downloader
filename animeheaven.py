#!/usr/bin/python3

import argparse
import codecs
import filelock
import itertools
import json
import lxml.html
import os
import os.path
import re
import requests
import shutil
import signal
import sys
import tqdm


class AnimeError(Exception):
    pass


class LimitReachedError(AnimeError):
    pass


class UpdateNecessaryError(AnimeError):
    pass


class AnimeHeaven:
    base_url = 'http://animeheaven.eu'
    anime_url = 'http://animeheaven.eu/i.php'  # a=<anime name>
    search_url = 'http://animeheaven.eu/search.php'  # q=<search query>
    watch_url = 'http://animeheaven.eu/watch.php'  # a=<anime name>&e=<episode>
    download_link_re = re.compile(r"var pli=rrl\(\"([^\"]+)\"\);")
    download_limit_re = re.compile(r"limit exceeded")
    link_substitions = dict(zip(
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
        'NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm'))

    @classmethod
    def search_anime(cls, search):
        def parse_info(element):
            episode_count = element\
                .cssselect('.iepst2, .iepst2r')[0].text_content()
            anime_name = element.cssselect('.cona')[0].text

            return {
                'episodes': int(episode_count),
                'name': anime_name,
            }

        response = requests.get(cls.search_url, params={'q': search})
        response.raise_for_status()

        return map(
            parse_info,
            lxml.html.fromstring(response.text).cssselect('.iepcon'))

    @classmethod
    def get_info(cls, anime_name, fuzzy=True):
        if fuzzy:
            return cls._get_info_fuzzy(anime_name)
        else:
            return cls._get_info_strict(anime_name)

    @classmethod
    def _get_info_fuzzy(cls, anime_name):
        animes = list(cls.search_anime(anime_name))
        if len(animes) == 1:
            return animes[0]
        else:
            for anime in animes:
                if anime['name'].lower() == anime_name.lower():
                    return anime

    @classmethod
    def _get_info_strict(cls, anime_name):
        response = requests.get(cls.anime_url, params={'a': anime_name})
        response.raise_for_status()

        episodes = lxml.html.fromstring(response.text).xpath(
            '//div[@class="textd" and text()="Episodes:"]'
        )[0].getnext().text

        return {
            'name': anime_name,
            'episodes': int(episodes),
        }

    @classmethod
    def get_episode(cls, anime_name, episode):
        params = {
            'a': anime_name,
            'e': episode,
        }
        response = requests.get(cls.watch_url, params=params)
        response.raise_for_status()

        if cls.download_limit_re.search(response.text):
            raise LimitReachedError

        download_link = cls.get_download_link(response)

        if download_link is None:
            raise UpdateNecessaryError

        return {
            'name': anime_name,
            'episode': int(episode),
            'source': download_link,
        }

    @classmethod
    def get_download_link(cls, response):
        encrypted_link = cls.download_link_re.search(response.text)

        if encrypted_link is None:
            return None

        def replace_chars(c):
            return cls.link_substitions.get(c, c)

        return ''.join(list(map(replace_chars, encrypted_link[1])))


class Range:
    def __init__(self, low, high=None):
        self.low = low
        self.high = high

    def __call__(self, episodes):
        if self.high is None:
            return [self.low]
        else:
            return range(self.low, self.high)


class Latest:
    def __init__(self, range):
        self.range = range

    def __call__(self, episodes):
        if self.range < 0:
            return range(max(1, episodes + self.range), episodes + 1)
        elif self.range > 0:
            return range(self.range, episodes + 1)
        else:
            return [episodes]


class All:
    def __call__(self, episodes):
        return range(1, episodes + 1)


def selection_type(value):
    def get_range(value):
        try:
            [low, high] = value.split('-')

            if low == 'latest':
                return Latest(-int(high))
            elif high == 'latest':
                return Latest(int(low))
            else:
                return Range(int(low), int(high) + 1)
        except ValueError:
            if value == 'latest':
                return Latest(0)
            else:
                return Range(int(value))

    ranges = list(map(get_range, value.split(',')))

    def with_episode_count(episodes):
        return sorted(set(itertools.chain.from_iterable(
            [r(episodes) for r in ranges]
        )))

    return with_episode_count


def progress_bar(response, initial=0):
    total_size = int(response.headers.get('content-length', 0)) + initial

    total_read = 0
    with tqdm.tqdm(
            total=total_size, initial=initial, unit='B',
            unit_scale=True, dynamic_ncols=True) as progress:
        for chunk in response.iter_content(2**16):
            progress.update(len(chunk))
            yield chunk


def download(anime, episode, naming_scheme, dest_dir):
    log_entry = "{} - {:03d}".format(anime, episode)
    basename = naming_scheme.format(name=anime, episode=episode)
    filename = "{}.mp4".format(basename)
    dest_file = os.path.join(dest_dir, filename)
    temp_file = os.path.join(dest_dir, '~{}'.format(filename))
    temp_lock = filelock.FileLock(f'{temp_file}.lock', timeout=0)

    if os.path.exists(dest_file):
        return
    elif os.path.exists(temp_file) and temp_lock.is_locked:
        return

    remove_lock = True

    try:
        info = AnimeHeaven.get_episode(anime, episode)

        headers = {}
        fsize = 0
        if os.path.exists(temp_file):
            fsize = os.stat(temp_file).st_size
            headers['Range'] = f'bytes={fsize}-'

        response = requests.get(info['source'], stream=True, headers=headers)
        response.raise_for_status()

        mode = 'ab' if response.status_code == 206 else 'wb'

        with temp_lock, open(temp_file, mode) as output:
            if response.status_code == 206:
                print("{}: Resuming".format(log_entry))
            else:
                print("{}: Downloading".format(log_entry))
                fsize = 0
            for chunk in progress_bar(response, fsize):
                output.write(chunk)

        shutil.move(temp_file, dest_file)

    except filelock.Timeout:
        remove_lock = False
    except KeyboardInterrupt:
        print("{}: Download canceled".format(log_entry))
        # Keep the file to be able to resume.
        raise
    finally:
        if remove_lock and os.path.exists(temp_lock.lock_file):
            os.remove(temp_lock.lock_file)


def raise_signal(signal, frame):
    raise KeyboardInterrupt


def main():
    argp = argparse.ArgumentParser()

    argp.add_argument(
        'anime', nargs='*',
        help='anime to search/download, fuzzy names are allowed, '
             'multiple arguments can be used instead of spaces')
    argp.add_argument(
        '-d', '--download', dest='download', action='store_true',
        default=False, help='download instead of search')
    argp.add_argument(
        '-D', '--dir', dest='dest_dir', default='.', help='download directory')
    argp.add_argument(
        '-n', '--naming-scheme', dest='naming_scheme',
        default='{name} - {episode:03d}',
        help='Pattern for filenames. Usable variables are {name} (string) and '
        '{episode} (integer). Follows python\'s format string syntax. '
        '(https://docs.python.org/3/library/string.html#formatstrings) '
        '(default: "%(default)s")')
    argp.add_argument(
        '-e', '--episodes', dest='episodes',
        type=selection_type, default=All(),
        help='select episodes to download (e.g.: '
             '"1,2,7-9,11-22", "latest", "55-latest", '
             '"latest-5" for 5 latest episodes)')

    argp.add_argument(
        '-c', '--config', dest='config', help='download specification file')

    argp.epilog = """
AnimeHeaven.eu has relatively low daily request limit.
You can bypass this limit by using proxy server.
To use proxy server, just export `HTTP_PROXY` environment variable.
"""

    args = argp.parse_args()
    signal.signal(signal.SIGTERM, raise_signal)

    try:
        if args.config:
            if not os.path.exists(args.config):
                argp.error(
                    'Specification file "{}" does not exist.'.format(
                        args.config))
            args.download = True
            with open(args.config) as spec_file:
                specs = json.load(spec_file)
            assert isinstance(specs, list)
            anime_specs = []
            for anime in specs:
                episodes = anime.get('episodes')
                episodes = selection_type(episodes) if episodes else All()
                dest_dir = os.path.expandvars(anime['dest_dir'])
                naming_scheme = anime.get('naming_scheme', args.naming_scheme)
                anime_specs.append(
                    (anime['name'], dest_dir, naming_scheme, episodes))
        else:
            anime_specs = [(
                ' '.join(args.anime),
                args.dest_dir,
                args.naming_scheme,
                args.episodes)]

        if args.download:
            for (anime_name, dest_dir, naming_scheme, episodes) in anime_specs:
                anime = AnimeHeaven.get_info(anime_name)
                if anime is None:
                    argp.exit(1, 'anime not found by given name\n')
                episodes = episodes(anime['episodes'])
                for episode in episodes:
                    download(
                        anime['name'], episode, naming_scheme, dest_dir)
        else:
            animes = AnimeHeaven.search_anime(args.anime)
            for anime in animes:
                print("{}\n  - Episodes: {}".format(
                    anime['name'],
                    anime['episodes']
                ))

    except UpdateNecessaryError:
        argp.exit(3, "Script update may be neccessary\n")
    except LimitReachedError:
        argp.exit(2, "Daily limit reached\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
