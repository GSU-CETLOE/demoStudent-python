# Examples

Runnable scripts that drive the library against stand-in data. No D2L
credentials needed, and nothing is changed.

| Example | What it shows |
| --- | --- |
| `findOrphanedDemoStudents.py` | Both detection passes: reading demo students out of D2L, then confirming which of their courses are gone. Shows why a course the service account cannot see is never treated as deleted |

Run one with:

```sh
uv run python examples/findOrphanedDemoStudents.py
```
