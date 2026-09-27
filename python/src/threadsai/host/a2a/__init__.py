"""Serving a host agent as an A2A 1.0 agent (spec/schema/a2a/). A context is a thread, a task is a
run, and every frame is a function of the committed log, so two reads of one state answer the same
bytes. The protocol itself is threadsai.a2a.protocol; nothing here re-parses it."""
