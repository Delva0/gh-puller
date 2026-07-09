## Claude Code Behavioral Rules

- **Voice Typing Tolerance**: The user inputs via Speech-to-Text. Ignore literal typos, homophones, or filler words. Comprehend the overall intent and context.
- **Strict Scope Control**: Do NOT perform any actions outside the explicit instructions. Never expand the task scope (e.g., do not auto-run/write tests, do not refactor adjacent code, and do not perform unsolicited exploration unless explicitly requested).
- **Direct Execution (No Sub-agents)**: Never spawn sub-agents (Plan, Explore, etc.) or generate complex multi-step automated workflows. Once the plan is mutually agreed upon with the user, implement the requested changes directly and immediately in the main conversation.
- **Interactive Alignment (Discuss Before Writing)**: Do not write code or implement changes immediately upon receiving a task. First, briefly state your understanding of the requirements and propose your planned approach. Wait for the user's confirmation or feedback before starting the implementation.
- **Python Execution via UV**: Always use `uv run` to execute Python scripts or commands. Do not use standard `python` or `python3` commands.
- **Code Style & Consistency**: Adopt a concise research or competitive programming style. Write zero redundant code with extremely minimal defensive programming. Write all comments in Chinese. Above all, maintain strict stylistic consistency with any existing core code in this project.
