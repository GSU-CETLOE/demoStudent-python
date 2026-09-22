# Examples

Runnable scripts that drive the library against stand-in data. No D2L
credentials needed, and nothing is changed.

| Example | What it shows |
| --- | --- |
| `findOrphanedDemoStudents.py` | Scanning for demo students whose course offering was deleted, and why a course the service account cannot see is never treated as one |

Run one with:

```sh
uv run python examples/findOrphanedDemoStudents.py
```
