# Milestones

The roadmap for ChemAssistent. Milestones are executed one at a time with the
`milestone-development` skill (`.claude/skills/milestone-development/SKILL.md`):
the first milestone with status `planned` is next. Design documents and worker
reports live in the git-ignored `dev/mN/`.

Format of a milestone:

```
## MN – <title>
**Status:** planned | in progress | done (#PR)
**Goal:** what the user gets.
**Work packages:**
- WPN.1 <summary> (modules)
**Acceptance criteria:**
- <checkable statement>
**Issues:** #n
**Open owner decisions:**
- <question>
```

## M1– Prompt distillation
**Status:** planned (draft – owner to confirm)
**Goal:** extract teaching data from saved conversations for fine-tuning a small model (#21).
**Work packages:**
- WP1.1 Export saved conversations (`history.py`) as training examples (to be designed)
**Acceptance criteria:**
- to be defined with the owner
**Issues:** #21
**Open owner decisions:**
- Scope, output format and privacy rules (conversations contain ELN data).
