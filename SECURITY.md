# Security

The Docker sandbox is a research design, not a hardened security boundary. Its path, SQL, process, and regression guards are exercised by tests, but time-of-check/time-of-use and host-process trust remain limitations. The local API has no authentication; keep it on loopback and do not expose it to untrusted users. For a security concern, open an issue with a brief non-sensitive summary and request a private reporting channel. Do not post exploit details publicly.
