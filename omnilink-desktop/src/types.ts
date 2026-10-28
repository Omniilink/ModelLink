export type Section = "chat" | "coding" | "workspaces" | "mcp" | "prompts" | "assets" | "settings";

export type ThemeMode = "dark" | "light" | "system";

export type Accent = "violet" | "cyan" | "emerald" | "rose" | "amber";

export type SettingsSection =
  | "appearance"
  | "models"
  | "data"
  | "account"
  | "analytics"
  | "updates"
  | "about";

export type MessageRole = "user" | "assistant" | "system";

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  createdAt: number;
  pinned?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  createdAt: number;
  updatedAt: number;
}

export interface ProjectFile {
  path: string;
  content: string;
}

export interface CodingProject {
  id: string;
  name: string;
  description: string;
  files: ProjectFile[];
  sample?: boolean;
  workspaceId?: string;
  createdAt: number;
  updatedAt: number;
}

export type McpTransport = "stdio" | "sse" | "http";

export interface McpServer {
  id: string;
  name: string;
  transport: McpTransport;
  command: string;
  args: string;
  url: string;
  enabled: boolean;
  createdAt: number;
}

export interface Prompt {
  id: string;
  title: string;
  description: string;
  body: string;
  tags: string[];
  createdAt: number;
  updatedAt: number;
}

export type ModelProvider = "ollama" | "openai" | "custom";

export interface Provider {
  id: string;
  name: string;
  baseUrl: string;
  model: string;
  hasKey: boolean;
  createdAt: number;
}

export interface Workspace {
  id: string;
  name: string;
  createdAt: number;
}

export interface Asset {
  id: string;
  name: string;
  kind: "image" | "model" | "other";
  path: string;
  createdAt: number;
}

export type ModalState =
  | { type: "project-new" }
  | { type: "prompt-new" }
  | { type: "prompt-edit"; id: string }
  | { type: "mcp-new" }
  | { type: "mcp-edit"; id: string }
  | null;

export interface Toast {
  id: string;
  title: string;
  description?: string;
  tone?: "default" | "success" | "danger";
}
