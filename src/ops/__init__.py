"""Low-level operations on the demo, shared by faultctl and the Action API.

The agent never imports this package: it observes through read-only tools and
changes the system only by calling the Action API over HTTP.
"""
