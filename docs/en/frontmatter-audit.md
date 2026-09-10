# Frontmatter audit fields

For `set_frontmatter`, both the receipt's `updated.fields` and the operation journal's
`parameters.fields` describe the lifecycle fields actually changed by that operation,
in the same order. Requested fields that already had the requested value are excluded.
The automatic `updated` timestamp is excluded from this list. A request with no lifecycle
change therefore reports an empty list and an empty journal field string.

[Translation](../fr/frontmatter-audit.md)
