"""What just happened, announced once, to whoever cares.

The shared middle kept reaching up. Signing out had to stop the phone camera,
stop realtime, hide the explorer, clear the history and reset three catalogs'
cost state; a catalog landing had to rebuild the sidebar's enum caches and
re-estimate; replacing the library had to reset the explorer's scroll. Every
one of those is a *feature reacting to a fact*, written as the fact's owner
calling the feature — which is what pinned `_set_auth_state`, the catalogs and
the history in `__init__.py` long after everything they need had moved out.

An emitter names what happened and knows nothing else. Subscriptions are made
in one place (`_wire_events` in the root), so the fan-out is still readable end
to end — it is a table now instead of a function.

Four rules, each of which is a bug that would otherwise be found the hard way:

- **Announce a fact, never an instruction.** `SIGNED_OUT`, not
  `shut_everything_down`. An emitter that names an action has just written the
  subscriber's half of the contract and taken the coupling back.
- **A raising subscriber must not stop the others.** These fire inline on the
  main thread from the auth transition; a broken viewport surface must not be
  able to take the sign-in down with it. Failures are logged and the fan-out
  continues, which is `safety.py`'s argument applied one layer up.
- **Topics are declared here and `on`/`emit` raise on anything else.** A typo
  would otherwise be a subscription that is never served, and nothing about a
  feature quietly not reacting looks like a bug.
- **`clear()` on unregister.** Subscribers are closures over modules the hot
  reload purges; left bound, the next load's emit would call into the previous
  load's dead package. `register()` re-wires from scratch.
"""

import logging

logger = logging.getLogger(__name__)

# Fired after every `set_auth_state`. It is the only topic left: the add-on
# has no account, so there is no signing in, no signing out, no workspace to
# change under a feature's feet, and no catalog or generation history to
# announce. The bus itself is kept because it is how the root's one table of
# reactions stays readable, and because the next thing worth announcing will
# want it.
AUTH_SETTLED = "auth_settled"

TOPICS = frozenset({AUTH_SETTLED})


_subscribers = {topic: [] for topic in TOPICS}


def on(topic, fn):
    """Subscribe `fn` to `topic`. Returns `fn`, so it also works as a decorator."""
    if topic not in TOPICS:
        raise KeyError(f"unknown event topic {topic!r}")
    _subscribers[topic].append(fn)
    return fn


def when(topic):
    """Decorator form of `on`, for a subscriber worth reading where it binds."""

    def bind(fn):
        return on(topic, fn)

    return bind


def emit(topic, **payload):
    """Tell every subscriber, and keep going if one of them raises."""
    if topic not in TOPICS:
        raise KeyError(f"unknown event topic {topic!r}")
    # A copy, so a subscriber may unsubscribe from inside its own callback.
    for fn in tuple(_subscribers[topic]):
        try:
            fn(**payload)
        except Exception:
            logger.exception("Scene Agent: %s subscriber failed", topic)


def clear():
    for subscribers in _subscribers.values():
        subscribers.clear()


def subscribers(topic):
    """For diagnostics and the live bridge: who is listening right now."""
    return tuple(_subscribers[topic])
