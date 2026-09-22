# Examples

Runnable scripts that demonstrate the library against stand-in data, so
they need no D2L credentials and change nothing.

| Example | What it shows |
| --- | --- |
| `findOrphanedDemoStudents.py` | Scanning for demo students whose course offering was deleted, including why a course the service account cannot see is never treated as deleted |

Run one with:

```sh
uv run python examples/findOrphanedDemoStudents.py
```
