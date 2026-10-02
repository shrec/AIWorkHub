# Editor-LM typed response repair: NF-2026-01255

The manager repairs only the Task MCP transport under self-hosting break-glass.

- Text and native consumers appended every string `part.value`, including typed thinking and data parts. VS Code distinguishes these from `LanguageModelTextPart`.
- The regression sends typed thinking/data containing JSON around a real text envelope through both production transports. Old code entered an ambiguous-envelope retry loop; the repaired bridge returns the actual answer in one turn and still counts every streamed part as provider progress.
- One shared filter accepts SDK text parts and legacy string/plain-object responses when no SDK text constructor exists. Native tool calls, cancellation, path/hash/range validation and authenticated review submission remain unchanged.
- Failure previews from reviewer `b667bff976da4ba7bf1a77764f88ab4e` and rework `b93fb8e4de7445a784eb320472f79276` contain reasoning. Their live part types were not recorded: attributing those particular failures to typed thinking remains a hypothesis until the replacement is activated.
- U1 was accepted independently as request `2a4315dbf40940b9b29914afee02cbb0` with verified DeepSeek correctness receipt, then committed as `77eba59`. The transport repair was serialized after U1 to preserve its exact promotion preimage.

Run the individual bridge harness and Python bridge/protocol/invariant/size tests. Install through an external hidden terminal; installation and activation are separate facts. U2/U3 still require their own candidate validation and live chat proof.
