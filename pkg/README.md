# devpi-smoke

A deliberately boring "hello world" package. Its only job is to be built, uploaded
to an internal devpi index, installed back out of it, and asked to prove that the
bytes that came back are the bytes that went in.

Each build stamps a unique `BUILD_ID` into the package, so a stale cache or a
silently-overwritten release is detectable rather than invisible.
