from core.eventing import EventManager
from core.helpers import all, merge, spawn, try_convert
from core.logger import Logger
from core.trakt import Trakt
from plex.plex_library import PlexLibrary
from plex.media_server_new import PlexMediaServer
from plex.metadata import PlexMetadata
from plex.plex_objects import PlexEpisode
from datetime import datetime
from threading import BoundedSemaphore
import traceback


log = Logger('sync.sync_base')


class Base(object):
    @classmethod
    def get_cache_id(cls):
        return EventManager.fire('sync.get_cache_id', single=True)


class PlexInterface(Base):
    @classmethod
    def sections(cls, types=None, keys=None):
        return PlexMediaServer.get_sections(types, keys, cache_id=cls.get_cache_id())

    @classmethod
    def library(cls, types=None, keys=None):
        return PlexLibrary.fetch(types, keys, cache_id=cls.get_cache_id())

    @classmethod
    def episodes(cls, key, parent=None):
        return PlexLibrary.fetch_episodes(key, parent, cache_id=cls.get_cache_id())

    @staticmethod
    def get_root(p_item):
        if isinstance(p_item, PlexEpisode):
            return p_item.parent

        return p_item

    @staticmethod
    def add_identifier(data, p_item):
        return PlexMetadata.add_identifier(data, p_item)

    @classmethod
    def to_trakt(cls, p_item, include_identifier=True):
        data = {}

        # Append episode attributes if this is a PlexEpisode
        if isinstance(p_item, PlexEpisode):
            data.update({
                'season': p_item.season_num,
                'episode': p_item.episode_num
            })

        if include_identifier:
            p_root = cls.get_root(p_item)

            data.update({
                'title': p_root.title,
                'year': p_root.year
            })

            cls.add_identifier(data, p_root)

        return data


class TraktInterface(Base):
    @classmethod
    def merged(cls, media, watched=True, ratings=False, collected=False, extended='min'):
        return Trakt.User.get_merged(media, watched, ratings, collected, extended, cache_id=cls.get_cache_id())


class SyncBase(Base):
    key = None
    task = None
    title = "Unknown"
    children = []

    auto_run = True
    threaded = False

    plex = PlexInterface
    trakt = TraktInterface

    def __init__(self, manager, parent=None):
        self.manager = manager
        self.parent = parent

        # Activate children and create dictionary map
        self.children = dict([(x.key, x(manager, self)) for x in self.children])

        self.artifacts = {}

        self.start_time = None

    def reset(self, artifacts=None):
        self.start_time = datetime.utcnow()

        self.artifacts = artifacts.copy() if artifacts else {}

        for child in self.children.itervalues():
            child.reset(artifacts)

    def run(self, *args, **kwargs):
        self.reset(kwargs.get('artifacts'))

        # Trigger handlers and return if there was an error
        if not all(self.trigger(None, *args, **kwargs)):
            self.update_status(False)
            return False

        # Trigger children and return if there was an error
        if not all(self.trigger_children(*args, **kwargs)):
            self.update_status(False)
            return False

        self.update_status(True)
        return True

    def child(self, name):
        return self.children.get(name)

    def trigger(self, funcs=None, *args, **kwargs):
        single = kwargs.pop('single', False)

        if funcs is None:
            funcs = [x[4:] for x in dir(self) if x.startswith('run_')]
        elif type(funcs) is not list:
            funcs = [funcs]

        # Get references to functions
        funcs = [(name, getattr(self, 'run_' + name)) for name in funcs if hasattr(self, 'run_' + name)]

        return self.trigger_run(funcs, single, *args, **kwargs)

    def trigger_children(self, *args, **kwargs):
        single = kwargs.pop('single', False)

        children = [(child.key, child.run) for (_, child) in self.children.items() if child.auto_run]

        return self.trigger_run(children, single, *args, **kwargs)

    def trigger_run(self, funcs, single, *args, **kwargs):
        if not funcs:
            return []

        if self.threaded:
            # Create lock and spawn functions
            lock = BoundedSemaphore(len(funcs))
            results = []

            for name, func in funcs:
                spawn(
                    self.trigger_spawn,
                    lock, results, func,

                    thread_name='sync.%s.%s' % (self.key, name),
                    *args, **kwargs
                )

            # Wait until everything is complete
            for x in range(len(funcs)):
                lock.acquire()

            return results

        # Run each task and collect results
        results = [func(*args, **kwargs) for (_, func) in funcs]

        if not single:
            return results

        return results[0]

    @staticmethod
    def trigger_spawn(lock, results, func, *args, **kwargs):
        lock.acquire()

        try:
            results.append(func(*args, **kwargs))
        except Exception, e:
            log.warn('Exception raised in triggered function %s (%s) %s: %s' % (
                func, type(e), e, traceback.format_exc()
            ))

        lock.release()

    @staticmethod
    def update_progress(current, start=0, end=100):
        raise ReferenceError()

    @staticmethod
    def is_stopping():
        raise ReferenceError()

    @staticmethod
    def get_enabled_functions():
        result = []

        if Prefs['sync_watched']:
            result.append('watched')

        if Prefs['sync_ratings']:
            result.append('ratings')

        if Prefs['sync_collection']:
            result.append('collected')

        return result

    def update_status(self, success, end_time=None, start_time=None, section=None):
        if end_time is None:
            end_time = datetime.utcnow()

        # Update task status
        status = self.get_status(section)
        status.update(success, start_time or self.start_time, end_time)

        log.info(
            'Task "%s" finished - success: %s, start: %s, elapsed: %s',
            status.key,
            status.previous_success,
            status.previous_timestamp,
            status.previous_elapsed
        )

    def get_status(self, section=None):
        """Retrieve the status of the current syncing task.

        :rtype : SyncStatus
        """
        if section is None:
            # Determine section from current state
            task, _ = self.get_current()
            if task is None:
                return None

            section = task.kwargs.get('section')

        return self.manager.get_status(self.task or self.key, section)

    def get_current(self):
        return self.manager.get_current()

    def retrieve(self, key, single=False):
        if single:
            return self.artifacts.get(key)

        return self.artifacts.get(key, [])

    def store(self, key, data, single=False):
        if single:
            self.artifacts[key] = data
            return

        if key not in self.artifacts:
            self.artifacts[key] = []

        self.artifacts[key].append(data)

    def store_episodes(self, key, show, episodes=None, artifact=None):
        if episodes is None:
            episodes = self.child('episode').artifacts.get(artifact or key)

        if episodes is None:
            return

        self.store(key, merge({'episodes': episodes}, show))