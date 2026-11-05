# Chemotion API quick reference

The `instance` variable is a logged-in `chemotion_api.Instance`. Objects are
modified locally and must be saved to persist changes. Look up the installed
chemotion-api documentation for details before using methods not listed here.

```python
root = instance.get_root_collection()
collection = root.get_collection('/Project name')
samples = collection.get_samples()
reactions = collection.get_reactions()
sample = instance.get_sample(123)
reaction = instance.get_reaction(123)
# For saved elements: element.url() returns the browser URL.
```

Samples can be created with `collection.new_sample_smiles('CCO')`, and reactions
with `collection.new_reaction()`. Both must be saved with `.save()`. Do not
create or modify records unless the user asks and approves the code.
