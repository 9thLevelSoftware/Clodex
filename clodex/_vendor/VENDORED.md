# Vendored dependencies

Clodex runs from whatever system Python the npm launcher finds, so pure-Python
dependencies are vendored here and used only when they are not importable.

## PyYAML 6.0.3 (`yaml/`)

- Source: https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz
- sdist sha256: `d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`
- License: MIT (`yaml/LICENSE`)
- Copied unmodified from `lib/yaml/*.py`, except `cyaml.py` is omitted (the optional
  libyaml bindings); `yaml/__init__.py` already falls back to the pure-Python
  loaders when `.cyaml` cannot be imported.
- To update: download the new sdist, verify its sha256, replace the files, update this note.
