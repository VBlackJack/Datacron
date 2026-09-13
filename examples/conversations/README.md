# Conversation acceptance cases

These fictional cases describe expected retrieval and write-verification behavior for
project resumption, meeting preparation, ambiguous identities, missing information,
replaced decisions and outstanding commitments. They contain no real correspondence.

Use `scripts/evaluate_conversation_trace.py` to grade a recorded tool trace against a
case. See the [daily workflow guide](../../docs/en/daily-workflows.md) for the trace
format and command. The grader checks supplied evidence; it does not contact a model
endpoint or prove that a trace came from a particular client.

Keep actual vault exports and recorded sessions under the ignored `local/` directory.
Only fictional, reviewed fixtures belong in this directory.
