"""
A registry of the handlers, attached to the resources or events.

The global registry is populated by the `kopf.on` decorators, and is used
to register the resources being watched and handled, and to attach
the handlers to the specific causes (create/update/delete/field-change).

The simple registry is part of the global registry (for each individual
resource), and also used for the sub-handlers within a top-level handler.

Both are used in the `kopf.reactor.handling` to retrieve the list
of the handlers to be executed on each reaction cycle.
"""
import abc
import collections
import functools
from types import FunctionType, MethodType
from typing import (Any, MutableMapping, Optional, Sequence, Collection, Iterable, Iterator,
                    List, Set, FrozenSet, Mapping, Callable, cast, Generic, TypeVar)

from kopf.reactor import callbacks
from kopf.reactor import causation
from kopf.reactor import errors as errors_
from kopf.reactor import handlers
from kopf.reactor import invocation
from kopf.structs import bodies
from kopf.structs import dicts
from kopf.structs import resources as resources_
from kopf.utilities import piggybacking

# We only type-check for known classes of handlers/callbacks, and ignore any custom subclasses.
HandlerFnT = TypeVar('HandlerFnT', callbacks.ActivityHandlerFn, callbacks.ResourceHandlerFn)
HandlerT = TypeVar('HandlerT', handlers.ActivityHandler, handlers.ResourceHandler)
CauseT = TypeVar('CauseT', bound=causation.BaseCause)


class GenericRegistry(Generic[HandlerT, HandlerFnT]):
    """ A generic base class of a simple registry (with no handler getters). """
    _handlers: List[HandlerT]

    def __init__(self) -> None:
        super().__init__()
        self._handlers = []

    def __bool__(self) -> bool:
        return bool(self._handlers)

    def append(self, handler: HandlerT) -> None:
        self._handlers.append(handler)


class ActivityRegistry(GenericRegistry[handlers.ActivityHandler, callbacks.ActivityHandlerFn]):
    """ An actual registry of activity handlers. """

    def register(
            self,
            fn: callbacks.ActivityHandlerFn,
            *,
            id: Optional[str] = None,
            errors: Optional[errors_.ErrorsMode] = None,
            timeout: Optional[float] = None,
            retries: Optional[int] = None,
            backoff: Optional[float] = None,
            cooldown: Optional[float] = None,  # deprecated, use `backoff`
            activity: Optional[causation.Activity] = None,
            _fallback: bool = False,
    ) -> callbacks.ActivityHandlerFn:
        real_id = generate_id(fn=fn, id=id)
        handler = handlers.ActivityHandler(
            id=real_id, fn=fn, activity=activity,
            errors=errors, timeout=timeout, retries=retries, backoff=backoff, cooldown=cooldown,
            _fallback=_fallback,
        )
        self.append(handler)
        return fn

    def get_handlers(
            self,
            activity: causation.Activity,
    ) -> Sequence[handlers.ActivityHandler]:
        return list(_deduplicated(self.iter_handlers(activity=activity)))

    def iter_handlers(
            self,
            activity: causation.Activity,
    ) -> Iterator[handlers.ActivityHandler]:
        found: bool = False

        # Regular handlers go first.
        for handler in self._handlers:
            if handler.activity is None or handler.activity == activity and not handler._fallback:
                yield handler
                found = True

        # Fallback handlers -- only if there were no matching regular handlers.
        if not found:
            for handler in self._handlers:
                if handler.activity is None or handler.activity == activity and handler._fallback:
                    yield handler


class ResourceRegistry(GenericRegistry[handlers.ResourceHandler, callbacks.ResourceHandlerFn], Generic[CauseT]):
    """ An actual registry of resource handlers. """

    def register(
            self,
            fn: callbacks.ResourceHandlerFn,
            *,
            id: Optional[str] = None,
            reason: Optional[causation.Reason] = None,
            event: Optional[str] = None,  # deprecated, use `reason`
            field: Optional[dicts.FieldSpec] = None,
            errors: Optional[errors_.ErrorsMode] = None,
            timeout: Optional[float] = None,
            retries: Optional[int] = None,
            backoff: Optional[float] = None,
            cooldown: Optional[float] = None,  # deprecated, use `backoff`
            initial: Optional[bool] = None,
            deleted: Optional[bool] = None,
            requires_finalizer: bool = False,
            labels: Optional[bodies.Labels] = None,
            annotations: Optional[bodies.Annotations] = None,
            when: Optional[callbacks.WhenHandlerFn] = None,
    ) -> callbacks.ResourceHandlerFn:
        if reason is None and event is not None:
            reason = causation.Reason(event)

        real_field = dicts.parse_field(field) or None  # to not store tuple() as a no-field case.
        real_id = generate_id(fn=fn, id=id, suffix=".".join(real_field or []))
        handler = handlers.ResourceHandler(
            id=real_id, fn=fn, reason=reason, field=real_field,
            errors=errors, timeout=timeout, retries=retries, backoff=backoff, cooldown=cooldown,
            initial=initial, deleted=deleted, requires_finalizer=requires_finalizer,
            labels=labels, annotations=annotations, when=when,
        )

        self.append(handler)
        return fn

    def get_handlers(
            self,
            cause: CauseT,
    ) -> Sequence[handlers.ResourceHandler]:
        return list(_deduplicated(self.iter_handlers(cause=cause)))

    @abc.abstractmethod
    def iter_handlers(
            self,
            cause: CauseT,
    ) -> Iterator[handlers.ResourceHandler]:
        raise NotImplementedError

    def get_extra_fields(
            self,
    ) -> Set[dicts.FieldPath]:
        return set(self.iter_extra_fields())

    def iter_extra_fields(
            self,
    ) -> Iterator[dicts.FieldPath]:
        for handler in self._handlers:
            if handler.field:
                yield handler.field

    def requires_finalizer(
            self,
            cause: causation.ResourceCause,
    ) -> bool:
        # check whether the body matches a deletion handler
        for handler in self._handlers:
            if handler.requires_finalizer and match(handler=handler, cause=cause):
                return True

        return False


class ResourceWatchingRegistry(ResourceRegistry[causation.ResourceWatchingCause]):

    def iter_handlers(
            self,
            cause: causation.ResourceWatchingCause,
    ) -> Iterator[handlers.ResourceHandler]:
        for handler in self._handlers:
            if match(handler=handler, cause=cause, ignore_fields=True):
                yield handler


class ResourceChangingRegistry(ResourceRegistry[causation.ResourceChangingCause]):

    def iter_handlers(
            self,
            cause: causation.ResourceChangingCause,
    ) -> Iterator[handlers.ResourceHandler]:
        changed_fields = frozenset(field for _, field, _, _ in cause.diff or [])
        for handler in self._handlers:
            if handler.reason is None or handler.reason == cause.reason:
                if handler.initial and not cause.initial:
                    pass  # ignore initial handlers in non-initial causes.
                elif handler.initial and cause.deleted and not handler.deleted:
                    pass  # ignore initial handlers on deletion, unless explicitly marked as usable.
                elif match(handler=handler, cause=cause, changed_fields=changed_fields):
                    yield handler


class OperatorRegistry:
    """
    A global registry is used for handling of multiple resources & activities.

    It is usually populated by the ``@kopf.on...`` decorators, but can also
    be explicitly created and used in the embedded operators.
    """
    _activity_handlers: ActivityRegistry
    _resource_watching_handlers: MutableMapping[resources_.Resource, ResourceWatchingRegistry]
    _resource_changing_handlers: MutableMapping[resources_.Resource, ResourceChangingRegistry]

    def __init__(self) -> None:
        super().__init__()
        self._activity_handlers = ActivityRegistry()
        self._resource_watching_handlers = collections.defaultdict(ResourceWatchingRegistry)
        self._resource_changing_handlers = collections.defaultdict(ResourceChangingRegistry)

    @property
    def resources(self) -> FrozenSet[resources_.Resource]:
        """ All known resources in the registry. """
        return frozenset(self._resource_watching_handlers) | frozenset(self._resource_changing_handlers)

    def register_activity_handler(
            self,
            fn: callbacks.ActivityHandlerFn,
            *,
            id: Optional[str] = None,
            errors: Optional[errors_.ErrorsMode] = None,
            timeout: Optional[float] = None,
            retries: Optional[int] = None,
            backoff: Optional[float] = None,
            cooldown: Optional[float] = None,  # deprecated, use `backoff`
            activity: Optional[causation.Activity] = None,
            _fallback: bool = False,
    ) -> callbacks.ActivityHandlerFn:
        return self._activity_handlers.register(
            fn=fn, id=id, activity=activity,
            errors=errors, timeout=timeout, retries=retries, backoff=backoff, cooldown=cooldown,
            _fallback=_fallback,
        )

    def register_resource_watching_handler(
            self,
            group: str,
            version: str,
            plural: str,
            fn: callbacks.ResourceHandlerFn,
            id: Optional[str] = None,
            labels: Optional[bodies.Labels] = None,
            annotations: Optional[bodies.Annotations] = None,
            when: Optional[callbacks.WhenHandlerFn] = None,
    ) -> callbacks.ResourceHandlerFn:
        """
        Register an additional handler function for low-level events.
        """
        resource = resources_.Resource(group, version, plural)
        return self._resource_watching_handlers[resource].register(
            fn=fn, id=id,
            labels=labels, annotations=annotations, when=when,
        )

    def register_resource_changing_handler(
            self,
            group: str,
            version: str,
            plural: str,
            fn: callbacks.ResourceHandlerFn,
            id: Optional[str] = None,
            reason: Optional[causation.Reason] = None,
            event: Optional[str] = None,  # deprecated, use `reason`
            field: Optional[dicts.FieldSpec] = None,
            errors: Optional[errors_.ErrorsMode] = None,
            timeout: Optional[float] = None,
            retries: Optional[int] = None,
            backoff: Optional[float] = None,
            cooldown: Optional[float] = None,  # deprecated, use `backoff`
            initial: Optional[bool] = None,
            deleted: Optional[bool] = None,
            requires_finalizer: bool = False,
            labels: Optional[bodies.Labels] = None,
            annotations: Optional[bodies.Annotations] = None,
            when: Optional[callbacks.WhenHandlerFn] = None,
    ) -> callbacks.ResourceHandlerFn:
        """
        Register an additional handler function for the specific resource and specific reason.
        """
        resource = resources_.Resource(group, version, plural)
        return self._resource_changing_handlers[resource].register(
            reason=reason, event=event, field=field, fn=fn, id=id,
            errors=errors, timeout=timeout, retries=retries, backoff=backoff, cooldown=cooldown,
            initial=initial, deleted=deleted, requires_finalizer=requires_finalizer,
            labels=labels, annotations=annotations, when=when,
        )

    def has_activity_handlers(
            self,
    ) -> bool:
        return bool(self._activity_handlers)

    def has_resource_watching_handlers(
            self,
            resource: resources_.Resource,
    ) -> bool:
        return (resource in self._resource_watching_handlers and
                bool(self._resource_watching_handlers[resource]))

    def has_resource_changing_handlers(
            self,
            resource: resources_.Resource,
    ) -> bool:
        return (resource in self._resource_changing_handlers and
                bool(self._resource_changing_handlers[resource]))

    def get_activity_handlers(
            self,
            *,
            activity: causation.Activity,
    ) -> Sequence[handlers.ActivityHandler]:
        return list(_deduplicated(self.iter_activity_handlers(activity=activity)))

    def get_resource_watching_handlers(
            self,
            cause: causation.ResourceWatchingCause,
    ) -> Sequence[handlers.ResourceHandler]:
        return list(_deduplicated(self.iter_resource_watching_handlers(cause=cause)))

    def get_resource_changing_handlers(
            self,
            cause: causation.ResourceChangingCause,
    ) -> Sequence[handlers.ResourceHandler]:
        return list(_deduplicated(self.iter_resource_changing_handlers(cause=cause)))

    def iter_activity_handlers(
            self,
            *,
            activity: causation.Activity,
    ) -> Iterator[handlers.ActivityHandler]:
        yield from self._activity_handlers.iter_handlers(activity=activity)

    def iter_resource_watching_handlers(
            self,
            cause: causation.ResourceWatchingCause,
    ) -> Iterator[handlers.ResourceHandler]:
        """
        Iterate all handlers for the low-level events.
        """
        if cause.resource in self._resource_watching_handlers:
            yield from self._resource_watching_handlers[cause.resource].iter_handlers(cause=cause)

    def iter_resource_changing_handlers(
            self,
            cause: causation.ResourceChangingCause,
    ) -> Iterator[handlers.ResourceHandler]:
        """
        Iterate all handlers that match this cause/event, in the order they were registered (even if mixed).
        """
        if cause.resource in self._resource_changing_handlers:
            yield from self._resource_changing_handlers[cause.resource].iter_handlers(cause=cause)

    def get_extra_fields(
            self,
            resource: resources_.Resource,
    ) -> Set[dicts.FieldPath]:
        return set(self.iter_extra_fields(resource=resource))

    def iter_extra_fields(
            self,
            resource: resources_.Resource,
    ) -> Iterator[dicts.FieldPath]:
        if resource in self._resource_changing_handlers:
            yield from self._resource_changing_handlers[resource].iter_extra_fields()

    def requires_finalizer(
            self,
            resource: resources_.Resource,
            cause: causation.ResourceCause,
    ) -> bool:
        """
        Check whether a finalizer should be added to the given resource or not.
        """
        return (resource in self._resource_changing_handlers and
                self._resource_changing_handlers[resource].requires_finalizer(cause=cause))


class SmartOperatorRegistry(OperatorRegistry):

    def __init__(self) -> None:
        super().__init__()

        try:
            import pykube
        except ImportError:
            pass
        else:
            self.register_activity_handler(
                id='login_via_pykube',
                fn=cast(callbacks.ActivityHandlerFn, piggybacking.login_via_pykube),
                activity=causation.Activity.AUTHENTICATION,
                errors=errors_.ErrorsMode.IGNORED,
                _fallback=True,
            )

        try:
            import kubernetes
        except ImportError:
            pass
        else:
            self.register_activity_handler(
                id='login_via_client',
                fn=cast(callbacks.ActivityHandlerFn, piggybacking.login_via_client),
                activity=causation.Activity.AUTHENTICATION,
                errors=errors_.ErrorsMode.IGNORED,
                _fallback=True,
            )


def generate_id(
        fn: Callable[..., Any],
        id: Optional[str],
        prefix: Optional[str] = None,
        suffix: Optional[str] = None,
) -> handlers.HandlerId:
    real_id: str
    real_id = id if id is not None else get_callable_id(fn)
    real_id = real_id if not suffix else f'{real_id}/{suffix}'
    real_id = real_id if not prefix else f'{prefix}/{real_id}'
    return cast(handlers.HandlerId, real_id)


def get_callable_id(c: Callable[..., Any]) -> str:
    """ Get an reasonably good id of any commonly used callable. """
    if c is None:
        raise ValueError("Cannot build a persistent id of None.")
    elif isinstance(c, functools.partial):
        return get_callable_id(c.func)
    elif hasattr(c, '__wrapped__'):  # @functools.wraps()
        return get_callable_id(getattr(c, '__wrapped__'))
    elif isinstance(c, FunctionType) and c.__name__ == '<lambda>':
        # The best we can do to keep the id stable across the process restarts,
        # assuming at least no code changes. The code changes are not detectable.
        line = c.__code__.co_firstlineno
        path = c.__code__.co_filename
        return f'lambda:{path}:{line}'
    elif isinstance(c, (FunctionType, MethodType)):
        return str(getattr(c, '__qualname__', getattr(c, '__name__', repr(c))))
    else:
        raise ValueError(f"Cannot get id of {c!r}.")


def _deduplicated(
        handlers: Iterable[HandlerT],
) -> Iterator[HandlerT]:
    """
    Yield the handlers deduplicated.

    The same handler function should not be invoked more than once for one
    single event/cause, even if it is registered with multiple decorators
    (e.g. different filtering criteria or different but same-effect causes).

    One of the ways how this could happen::

        @kopf.on.create(...)
        @kopf.on.resume(...)
        def fn(**kwargs): pass

    In normal cases, the function will be called either on resource creation
    or on operator restart for the pre-existing (already handled) resources.
    When a resource is created during the operator downtime, it is
    both creation and resuming at the same time: the object is new (not yet
    handled) **AND** it is detected as per-existing before operator start.
    But `fn()` should be called only once for this cause.
    """
    seen_ids: Set[int] = set()
    for handler in handlers:
        if id(handler.fn) in seen_ids:
            pass
        else:
            seen_ids.add(id(handler.fn))
            yield handler


def match(
        handler: handlers.ResourceHandler,
        cause: causation.ResourceCause,
        changed_fields: Collection[dicts.FieldPath] = frozenset(),
        ignore_fields: bool = False,
) -> bool:
    return all([
        _matches_field(handler, changed_fields or {}, ignore_fields),
        _matches_labels(handler, cause.body),
        _matches_annotations(handler, cause.body),
        _matches_filter_callback(handler, cause),
    ])


def _matches_field(
        handler: handlers.ResourceHandler,
        changed_fields: Collection[dicts.FieldPath] = frozenset(),
        ignore_fields: bool = False,
) -> bool:
    return (ignore_fields or
            not handler.field or
            any(field[:len(handler.field)] == handler.field for field in changed_fields))


def _matches_labels(
        handler: handlers.ResourceHandler,
        body: bodies.Body,
) -> bool:
    return (not handler.labels or
            _matches_metadata(pattern=handler.labels,
                              content=body.get('metadata', {}).get('labels', {})))


def _matches_annotations(
        handler: handlers.ResourceHandler,
        body: bodies.Body,
) -> bool:
    return (not handler.annotations or
            _matches_metadata(pattern=handler.annotations,
                              content=body.get('metadata', {}).get('annotations', {})))


def _matches_metadata(
        *,
        pattern: Mapping[str, str],  # from the handler
        content: Mapping[str, str],  # from the body
) -> bool:
    for key, value in pattern.items():
        if key not in content:
            return False
        elif value is not None and value != content[key]:
            return False
        else:
            continue
    return True


def _matches_filter_callback(
        handler: handlers.ResourceHandler,
        cause: causation.ResourceCause,
) -> bool:
    if not handler.when:
        return True
    return handler.when(**invocation.get_invoke_arguments(cause=cause))


_default_registry: Optional[OperatorRegistry] = None


def get_default_registry() -> OperatorRegistry:
    """
    Get the default registry to be used by the decorators and the reactor
    unless the explicit registry is provided to them.
    """
    global _default_registry
    if _default_registry is None:
        # TODO: Deprecated registry to ensure backward-compatibility until removal:
        from kopf.toolkits.legacy_registries import SmartGlobalRegistry
        _default_registry = SmartGlobalRegistry()
    return _default_registry


def set_default_registry(registry: OperatorRegistry) -> None:
    """
    Set the default registry to be used by the decorators and the reactor
    unless the explicit registry is provided to them.
    """
    global _default_registry
    _default_registry = registry
