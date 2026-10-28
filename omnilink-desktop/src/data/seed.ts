import type {
  CodingProject,
  Conversation,
  McpServer,
  Prompt,
} from "@/types";

const now = Date.now();

export const welcomeConversation: Conversation = {
  id: "welcome",
  title: "Welcome to OmniLink",
  createdAt: now - 120_000,
  updatedAt: now - 60_000,
  messages: [
    {
      id: "welcome-message",
      role: "assistant",
      createdAt: now - 60_000,
      content: [
        "# Welcome to OmniLink",
        "",
        "One local-first workspace for chat, code, MCP tools, and the prompts you reuse.",
        "",
        "## Get oriented",
        "",
        "- **Chat** — conversations stay on this device until you connect sync.",
        "- **Coding** — create a project and edit files in the built-in editor.",
        "- **MCP** — register stdio, SSE, and streamable HTTP servers.",
        "- **Prompts** — keep reusable instructions one click away.",
        "",
        "Press `Ctrl K` to jump anywhere. Press `Ctrl N` for a new chat.",
        "",
        "> Model connections live in **Settings → Models**. Add a local Ollama endpoint or connect the free OpenCode Zen tier.",
      ].join("\n"),
    },
  ],
};

export const gettingStartedProject: CodingProject = {
  id: "getting-started",
  name: "getting-started",
  description: "A small scratch project for trying the editor.",
  sample: true,
  createdAt: now - 86_400_000,
  updatedAt: now - 3_600_000,
  files: [
    {
      path: "README.md",
      content: `# Getting started

This is a local scratch project. Files are stored with your workspace and
can be edited in the built-in editor.

## Try it

1. Open \`src/example.ts\` in the file tree.
2. Change the task list.
3. Press \`Ctrl S\` to save.

Everything here is local to this device. Create a new project from the
Coding pane whenever you want a clean workspace.
`,
    },
    {
      path: "src/example.ts",
      content: `type Task = {
  id: string;
  title: string;
  done: boolean;
};

export const tasks: Task[] = [
  { id: "1", title: "Connect a model", done: false },
  { id: "2", title: "Create a coding project", done: true },
  { id: "3", title: "Register an MCP server", done: false },
];

export function completion(tasks: Task[]): number {
  if (tasks.length === 0) return 0;
  const finished = tasks.filter((task) => task.done).length;
  return Math.round((finished / tasks.length) * 100);
}

console.log(\`Workspace ready: \${completion(tasks)}%\`);
`,
    },
    {
      path: "package.json",
      content: `{
  "name": "getting-started",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build"
  }
}
`,
    },
  ],
};

export const seedPrompts: Prompt[] = [
  {
    id: "prompt-code-review",
    title: "Senior code review",
    description:
      "A strict but constructive review focused on correctness, edge cases, and maintainability.",
    tags: ["Coding", "Review"],
    createdAt: now - 604_800_000,
    updatedAt: now - 86_400_000,
    body: `Review the changes as a senior engineer.

Prioritize:
1. Correctness and unhandled edge cases.
2. Security, data loss, and race conditions.
3. Readability and unnecessary complexity.
4. Missing tests or weak assertions.

For every finding, give the file and line, explain the failure mode, and suggest a concrete fix. Call out anything that is already good. Do not rewrite the whole file unless the design is fundamentally wrong.`,
  },
  {
    id: "prompt-feature-plan",
    title: "Plan a feature end to end",
    description:
      "Turns a rough idea into scope, data flow, edge cases, and a build order.",
    tags: ["Planning", "Product"],
    createdAt: now - 518_400_000,
    updatedAt: now - 172_800_000,
    body: `Turn this idea into an implementation plan:

- Restate the user problem in one sentence.
- Define the smallest version that is genuinely useful.
- List the data model, API surface, and UI states.
- Call out failure modes, permissions, and empty states.
- Propose a build order with verifiable milestones.

Ask at most three clarifying questions first. Then produce the plan.`,
  },
  {
    id: "prompt-explain-code",
    title: "Explain this codebase",
    description:
      "Builds a mental model of an unfamiliar repository without dumping every file.",
    tags: ["Coding", "Learning"],
    createdAt: now - 432_000_000,
    updatedAt: now - 259_200_000,
    body: `Explain this codebase to a new contributor.

Start with the runtime shape: entry points, major modules, and how data moves
between them. Then describe the three most important flows end to end.

Be concrete about file paths and function names. Skip generated files,
vendored code, and anything that does not affect the main flows. Finish with
the first five places a new contributor should read.`,
  },
  {
    id: "prompt-commit-message",
    title: "Write a precise commit message",
    description:
      "Conventional commit subject plus a body that explains the why, not the diff.",
    tags: ["Git", "Writing"],
    createdAt: now - 345_600_000,
    updatedAt: now - 345_600_000,
    body: `Write a commit message for the staged changes.

Use a conventional commit subject under 72 characters. Add a body explaining
the problem, the chosen approach, and any user-visible behavior change. Mention
migrations, breaking changes, or follow-up work explicitly.

Do not describe the diff line by line. Return the message in a copyable block.`,
  },
  {
    id: "prompt-bug-triage",
    title: "Triage a bug report",
    description:
      "Separates reproduction from diagnosis and produces the smallest credible fix.",
    tags: ["Debugging", "Coding"],
    createdAt: now - 259_200_000,
    updatedAt: now - 259_200_000,
    body: `Triage this bug report.

1. Identify the expected behavior and the observed behavior.
2. List the missing information needed to reproduce it.
3. Form the two most likely hypotheses and the evidence that would confirm each.
4. Propose the narrowest instrumentation or test that distinguishes them.

Do not propose a fix until a hypothesis is supported by evidence.`,
  },
];

export const seedMcpServers: McpServer[] = [];
