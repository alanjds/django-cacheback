import time
import logging
import hashlib

from django.core.cache import get_cache, DEFAULT_CACHE_ALIAS
from django.conf import settings
import six

from cacheback import tasks

logger = logging.getLogger('cacheback')

MEMCACHE_MAX_EXPIRATION = 2592000


def to_bytestring(value):
    """
    Encode a string as a UTF8 bytestring.  This function could be passed a
    bytestring or unicode string so must distinguish between the two.
    """
    if isinstance(value, six.text_type):
        return string.encode('utf8')
    if isinstance(value, six.binary_type):
        return value
    if six.PY2:
        return str(value)
    return bytes(str(value), 'utf8')


class Job(object):
    """
    A cached read job.

    This is the core class for the package which is intended to be subclassed
    to allow the caching behaviour to be customised.
    """
    # All items are stored in memcache as a tuple (expiry, data).  We don't use
    # the TTL functionality within memcache but implement on own.  If the
    # expiry value is None, this indicates that there is already a job created
    # for refreshing this item.

    #: Default cache lifetime is 5 minutes.  After this time, the result will
    #: be considered stale and requests will trigger a job to refresh it.
    lifetime = 600

    #: Timeout period during which no new Celery tasks will be created for a
    #: single cache item.  This time should cover the normal time required to
    #: refresh the cache.
    refresh_timeout = 60

    #: Time to store items in the cache.  After this time, we will get a cache
    #: miss which can lead to synchronous refreshes if you have
    #: fetch_on_miss=True.
    cache_ttl = MEMCACHE_MAX_EXPIRATION

    #: Whether to perform a synchronous refresh when a result is missing from
    #: the cache.  Default behaviour is to do a synchronous fetch when the cache is empty.
    #: Stale results are generally ok, but not no results.
    fetch_on_miss = True

    #: Whether to perform a synchronous refresh when a result is in the cache
    #: but stale from. Default behaviour is never to do a synchronous fetch but
    #: there will be times when an item is _too_ stale to be returned.
    fetch_on_stale_threshold = None

    #: Overrides options for `refresh_cache.apply_async` (e.g. `queue`).
    task_options = {}

    def __init__(self):
        self.cache_alias = getattr(settings, 'CACHEBACK_CACHE_ALIAS', DEFAULT_CACHE_ALIAS)
        self.cache = get_cache(self.cache_alias)

    # --------
    # MAIN API
    # --------

    def get(self, *raw_args, **raw_kwargs):
        """
        Return the data for this function (using the cache if possible).

        This method is not intended to be overidden
        """
        # We pass args and kwargs through a filter to allow them to be
        # converted into values that can be pickled.
        args = self.prepare_args(*raw_args)
        kwargs = self.prepare_kwargs(**raw_kwargs)

        # Build the cache key and attempt to fetch the cached item
        key = self.key(*args, **kwargs)
        item = self.cache.get(key)

        if item is None:
            # Cache MISS - we can either:
            # a) fetch the data immediately, blocking execution until
            #    the fetch has finished, or
            # b) trigger an async refresh and return an empty result
            if self.should_missing_item_be_fetched_synchronously(*args, **kwargs):
                logger.debug(("Job %s with key '%s' - cache MISS - running "
                              "synchronous refresh"),
                             self.class_path, key)
                fetched = self.refresh(*args, **kwargs)
                return self.got_miss(fetched, False, *raw_args, **raw_kwargs)

            else:
                logger.debug(("Job %s with key '%s' - cache MISS - triggering "
                              "async refresh and returning empty result"),
                             self.class_path, key)
                # To avoid cache hammering (ie lots of identical Celery tasks
                # to refresh the same cache item), we reset the cache with an
                # empty result which will be returned until the cache is
                # refreshed.
                empty = self.empty()
                self.cache_set(key, self.timeout(*args, **kwargs), empty, 'QUEUED')
                self.async_refresh(*args, **kwargs)
                fetched = empty
                return self.got_miss(fetched, True, *raw_args, **raw_kwargs)

        expiry, data, status = item
        delta = time.time() - expiry
        if delta > 0:
            # Cache HIT but STALE expiry - we can either:
            # a) fetch the data immediately, blocking execution until
            #    the fetch has finished, or
            # b) trigger a refresh but allow the stale result to be
            #    returned this time.  This is normally acceptable.
            if self.should_stale_item_be_fetched_synchronously(
                    delta, *args, **kwargs):
                logger.debug(
                    ("Job %s with key '%s' - STALE cache hit - running "
                    "synchronous refresh"),
                    self.class_path, key)
                fetched = self.refresh(*args, **kwargs)
                return self.got_stale(fetched, False, *raw_args, **raw_kwargs)

            else:
                logger.debug(
                    ("Job %s with key '%s' - STALE cache hit - triggering "
                    "async refresh and returning stale result"),
                    self.class_path, key)
                # We replace the item in the cache with a 'timeout' expiry - this
                # prevents cache hammering but guards against a 'limbo' situation
                # where the refresh task fails for some reason.
                timeout = self.timeout(*args, **kwargs)
                self.cache_set(key, timeout, data, 'QUEUED')
                self.async_refresh(*args, **kwargs)
                fetched = data
                return self.got_stale(fetched, True, *raw_args, **raw_kwargs)
        else:
            logger.debug("Job %s with key '%s' - cache HIT (%s)", self.class_path, key, status)
            fetched = data
            return self.got_hit(fetched, status, *raw_args, **raw_kwargs)

    def invalidate(self, *raw_args, **raw_kwargs):
        """
        Mark a cached item invalid and trigger an asynchronous
        job to refresh the cache
        """
        args = self.prepare_args(*raw_args)
        kwargs = self.prepare_kwargs(**raw_kwargs)
        key = self.key(*args, **kwargs)
        item = self.cache.get(key)
        if item is not None:
            expiry, data = item
            self.cache_set(key, self.timeout(*args, **kwargs), data, 'QUEUED')
            self.async_refresh(*args, **kwargs)

    def delete(self, *raw_args, **raw_kwargs):
        """
        Remove an item from the cache
        """
        args = self.prepare_args(*raw_args)
        kwargs = self.prepare_kwargs(**raw_kwargs)
        key = self.key(*args, **kwargs)
        item = self.cache.get(key)
        if item is not None:
            self.cache.delete(key)

    # --------------
    # HELPER METHODS
    # --------------

    def prepare_args(self, *args):
        return args

    def prepare_kwargs(self, **kwargs):
        return kwargs

    def cache_set(self, key, expiry, status, data):
        """
        Add a result to the cache

        :key: Cache key to use
        :expiry: The expiry timestamp after which the result is stale
        :data: The data to cache
        """
        self.cache.set(key, (expiry, status, data), self.cache_ttl)

        if getattr(settings, 'CACHEBACK_VERIFY_CACHE_WRITE', True):
            # We verify that the item was cached correctly.  This is to avoid a
            # Memcache problem where some values aren't cached correctly
            # without warning.
            __, __, cached_data, = self.cache.get(key, (None, None, None))
            if data is not None and cached_data is None:
                raise RuntimeError(
                    "Unable to save data of type %s to cache" % (
                        type(data)))

    def refresh(self, *args, **kwargs):
        """
        Fetch the result SYNCHRONOUSLY and populate the cache
        """
        result = self.fetch(*args, **kwargs)
        self.cache_set(self.key(*args, **kwargs),
                       self.expiry(*args, **kwargs),
                       'FRESH',
                       result)
        return result

    def async_refresh(self, *args, **kwargs):
        """
        Trigger an asynchronous job to refresh the cache
        """
        # We trigger the task with the class path to import as well as the
        # (a) args and kwargs for instantiating the class
        # (b) args and kwargs for calling the 'refresh' method
        try:
            tasks.refresh_cache.apply_async(
                kwargs=dict(
                    klass_str=self.class_path,
                    obj_args=self.get_constructor_args(),
                    obj_kwargs=self.get_constructor_kwargs(),
                    call_args=args,
                    call_kwargs=kwargs
                ),
                **self.task_options
            )
        except Exception as e:
            # Handle exceptions from talking to RabbitMQ - eg connection
            # refused.  When this happens, we try to run the task
            # synchronously.
            logger.error("Unable to trigger task asynchronously - failing "
                         "over to synchronous refresh", exc_info=True)
            try:
                return self.refresh(*args, **kwargs)
            except Exception as e:
                # Something went wrong while running the task
                logger.error("Unable to refresh data synchronously: %s", e,
                             exc_info=True)
            else:
                logger.debug("Failover synchronous refresh completed successfully")

    def get_constructor_args(self):
        return ()

    def get_constructor_kwargs(self):
        """
        Return the kwargs that need to be passed to __init__ when
        reconstructing this class.
        """
        return {}

    @property
    def class_path(self):
        return '%s.%s' % (self.__module__, self.__class__.__name__)

    # Override these methods

    def empty(self):
        """
        Return the appropriate value for a cache MISS (and when we defer the
        repopulation of the cache)
        """
        return None

    def expiry(self, *args, **kwargs):
        """
        Return the expiry timestamp for this item.
        """
        return time.time() + self.lifetime

    def timeout(self, *args, **kwargs):
        """
        Return the refresh timeout for this item
        """
        return time.time() + self.refresh_timeout

    def should_missing_item_be_fetched_synchronously(self, *args, **kwargs):
        """
        Return whether to refresh an item synchronously when it is missing from
        the cache
        """
        return self.fetch_on_miss

    def should_item_be_fetched_synchronously(self, *args, **kwargs):
        import warnings
        warnings.warn(
            "The method 'should_item_be_fetched_synchronously' is deprecated "
            "and will be removed in 0.5.  Use "
            "'should_missing_item_be_fetched_synchronously' instead.",
            DeprecationWarning)
        return self.should_missing_item_be_fetched_synchronously(
            *args, **kwargs)

    def should_stale_item_be_fetched_synchronously(self, delta, *args, **kwargs):
        """
        Return whether to refresh an item synchronously when it is found in the
        cache but stale
        """
        if self.fetch_on_stale_threshold is None:
            return False
        return delta > (self.fetch_on_stale_threshold - self.lifetime)

    def key(self, *args, **kwargs):
        """
        Return the cache key to use.

        If you're passing anything but primitive types to the ``get`` method,
        it's likely that you'll need to override this method.
        """
        if not args and not kwargs:
            return self.class_path
        try:
            if args and not kwargs:
                return "%s:%s" % (self.class_path, self.hash(args))
            # The line might break if your passed values are un-hashable.  If
            # it does, you need to override this method and implement your own
            # key algorithm.
            return "%s:%s:%s:%s" % (self.class_path,
                                    self.hash(args),
                                    self.hash(tuple(kwargs.keys())),
                                    self.hash(tuple(kwargs.values())))
        except TypeError:
            raise RuntimeError(
                "Unable to generate cache key due to unhashable"
                "args or kwargs - you need to implement your own"
                "key generation method to avoid this problem")

    def hash(self, value):
        """
        Generate a hash of the given tuple.

        This is for use in a cache key.
        """
        return hashlib.md5(to_bytestring(value)).hexdigest()

    def fetch(self, *args, **kwargs):
        """
        Return the data for this job - this is where the expensive work should
        be done.
        """
        raise NotImplementedError()

    def got_miss(self, fetched, async, *raw_args, **raw_kwargs):
        """
        Transforms the fetched data right before returning from .get(...)
        Only runs if data is MISS.

        'async' is False for synchronous refreshes. True otherwise.
        """
        return fetched

    def got_hit(self, fetched, status, *raw_args, **raw_kwargs):
        """
        Transforms the fetched data right before returning from .get(...)
        Only runs if data is fresh HIT.

        'status' can be 'FRESH'. Can be 'QUEUED' to refresh too.
        """
        return fetched

    def got_stale(self, fetched, async, *raw_args, **raw_kwargs):
        """
        Transforms the fetched data right before returning from .get(...)
        Only runs if data is STALE.

        'async' is False for synchronous refreshes. True otherwise.
        """
        return fetched

