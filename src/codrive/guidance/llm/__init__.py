"""Optional Qwen/OpenAI providers for non-critical guidance wording.

Critical guidance always uses :mod:`codrive.guidance.policy` templates. Model
providers may rephrase an already-decided event, but must not alter its severity
or recommended action.
"""
