# Optional E4 decoder dependencies

`vininfo` 1.11.0 is Copyright Igor Starikov and contributors, licensed under
BSD-3-Clause: https://github.com/idlesign/vininfo/blob/master/LICENSE.

`@cardog/corgi` 2.0.4 is Copyright Cardog Team, licensed under ISC:
https://github.com/cardog-ai/corgi/blob/master/LICENSE.
Its database derives from NHTSA vPIC manufacturer-reported vehicle information.
The adapter and online vPIC are therefore members of the same data-origin group;
agreement between them is not independent confirmation. The snapshot's upstream
data date is not established by its package version.

The repository contains dependency manifests, integrity pins and this adapter,
not copies of the upstream Python package or the vehicle database. Explicit
preparation installs packages and creates a checked local database outside Git.
Decode neither downloads data nor installs packages.

Node's bundled SQLite adapter is used through Corgi's published browser core.
The older native `better-sqlite3` dependency is installed with lifecycle scripts
disabled and is never imported or executed by this adapter.
