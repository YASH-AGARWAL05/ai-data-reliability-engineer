# Security

The Docker sandbox is a research design, not a hardened security boundary. Its path, SQL, process, and regression guards are exercised by tests, but time-of-check/time-of-use and host-process trust remain limitations. The local API has no authentication; keep it on loopback and do not expose it to untrusted users. Report a security concern privately to the maintainer through GitHub rather than posting an exploit in a public issue.
