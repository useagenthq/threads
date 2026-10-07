# threads (TypeScript)

The TypeScript implementation of threads. It must pass everything in `../spec/conformance/`.
Rules: `../AGENTS.md` (Zod 4, strict tsconfig, Bun, Biome).

## Built-in Eval Scorers

`threads` provides three built-in evaluation scorers to grade your agent's runs without writing custom judges from scratch. These scorers can be used directly in a case's `expect` block or programmatically via `runEvals()`.

### 1. Faithfulness (`faithfulness`)
- **Type**: Judge-based (uses the case's configured LLM judge model).
- **Score Range**: `0.0` to `1.0`
- **Description**: Evaluates whether the generated response is grounded in and supported strictly by the provided context/retrieved documents. It detects hallucinations or claims not supported by the source text.

### 2. Answer Relevancy (`answerRelevancy`)
- **Type**: Judge-based (uses the case's configured LLM judge model).
- **Score Range**: `0.0` to `1.0`
- **Description**: Evaluates how relevant the generated response is to the user's input query. It penalizes redundant, incomplete, or off-topic answers.

### 3. Tool-Call Correctness (`toolCallCorrectness`)
- **Type**: Deterministic (no LLM calls).
- **Score Range**: `0.0` to `1.0`
- **Description**: Compares the tool calls executed during the run against the expected tool calls. It checks for tool name matches, argument correctness, and execution order.

---

## Usage Examples

### Using Scorers in a Case's `expect` Block

You can define these scorers directly inside your test cases. The judge-based scorers will automatically use the case's configured judge model.