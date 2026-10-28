import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import {
  gettingStartedProject,
  seedMcpServers,
  seedPrompts,
  welcomeConversation,
} from "@/data/seed";
import type {
  Accent,
  CodingProject,
  Conversation,
  McpServer,
  McpTransport,
  Message,
  ModalState,
  Prompt,
  Provider,
  ModelProvider,
  Section,
  SettingsSection,
  ThemeMode,
  Toast,
} from "@/types";
import { uid } from "@/lib/utils";
import { zustandStorage } from "@/lib/storage";

function scaffoldProject(name: string, description: string): CodingProject {
  const timestamp = Date.now();
  return {
    id: uid("project-"),
    name,
    description,
    createdAt: timestamp,
    updatedAt: timestamp,
    files: [
      {
        path: "README.md",
        content: `# ${name}\n\n${description || "A new OmniLink project."}\n\nCreated ${new Date().toLocaleDateString()}.\n`,
      },
      {
        path: "src/index.ts",
        content: `export function greet(name: string): string {\n  return \`Hello, \${name}.\`;\n}\n\nconsole.log(greet("OmniLink"));\n`,
      },
    ],
  };
}

interface AppState {
  section: Section;
  settingsSection: SettingsSection;
  theme: ThemeMode;
  accent: Accent;
  reduceMotion: boolean;
  contextCollapsed: boolean;
  paletteOpen: boolean;
  modal: ModalState;
  activeConversationId: string | null;
  activeProjectId: string | null;
  promptTag: string | null;
  ollamaUrl: string;
  modelProvider: ModelProvider;
  modelBaseUrl: string;
  modelApiKey: string;
  modelName: string;
  providers: Provider[];
  activeProviderId: string | null;
  autoCheckUpdates: boolean;

  conversations: Conversation[];
  projects: CodingProject[];
  prompts: Prompt[];
  mcpServers: McpServer[];
  toasts: Toast[];

  setSection: (section: Section) => void;
  openSettings: (section: SettingsSection) => void;
  setSettingsSection: (section: SettingsSection) => void;
  setTheme: (theme: ThemeMode) => void;
  setAccent: (accent: Accent) => void;
  setReduceMotion: (reduce: boolean) => void;
  toggleContext: () => void;
  setPaletteOpen: (open: boolean) => void;
  openModal: (modal: ModalState) => void;
  closeModal: () => void;

  newConversation: () => string;
  deleteConversation: (id: string) => void;
  appendMessage: (conversationId: string, message: Message) => void;
  renameConversation: (id: string, title: string) => void;
  setActiveConversation: (id: string | null) => void;

  addProject: (name: string, description: string) => string;
  deleteProject: (id: string) => void;
  updateProjectFile: (
    projectId: string,
    path: string,
    content: string,
  ) => void;
  setActiveProject: (id: string | null) => void;

  addMcpServer: (server: {
    name: string;
    transport: McpTransport;
    command: string;
    args: string;
    url: string;
  }) => void;
  updateMcpServer: (
    id: string,
    patch: Partial<Pick<McpServer, "name" | "command" | "args" | "url" | "transport">>,
  ) => void;
  toggleMcpServer: (id: string) => void;
  deleteMcpServer: (id: string) => void;

  addPrompt: (prompt: {
    title: string;
    description: string;
    body: string;
    tags: string[];
  }) => void;
  updatePrompt: (
    id: string,
    patch: Partial<Pick<Prompt, "title" | "description" | "body" | "tags">>,
  ) => void;
  deletePrompt: (id: string) => void;
  setPromptTag: (tag: string | null) => void;

  setOllamaUrl: (url: string) => void;
  setModelProvider: (provider: ModelProvider) => void;
  setModelBaseUrl: (url: string) => void;
  setModelApiKey: (key: string) => void;
  setModelName: (name: string) => void;
  addProvider: (provider: Omit<Provider, "id" | "createdAt">) => string;
  updateProvider: (id: string, patch: Partial<Provider>) => void;
  deleteProvider: (id: string) => void;
  setActiveProvider: (id: string | null) => void;
  updateMessage: (conversationId: string, messageId: string, content: string) => void;
  setAutoCheckUpdates: (value: boolean) => void;

  pushToast: (toast: Omit<Toast, "id">) => void;
  dismissToast: (id: string) => void;
  resetWorkspace: () => void;
}

export const useApp = create<AppState>()(
  persist(
    (set, get) => ({
      section: "chat",
      settingsSection: "appearance",
      theme: "dark",
      accent: "violet",
      reduceMotion: false,
      contextCollapsed: false,
      paletteOpen: false,
      modal: null,
      activeConversationId: welcomeConversation.id,
      activeProjectId: gettingStartedProject.id,
      promptTag: null,
      ollamaUrl: "http://127.0.0.1:11434",
      modelProvider: "ollama",
      modelBaseUrl: "http://127.0.0.1:11434",
      modelApiKey: "",
      modelName: "",
      providers: [],
      activeProviderId: null,
      autoCheckUpdates: true,

      conversations: [welcomeConversation],
      projects: [gettingStartedProject],
      prompts: seedPrompts,
      mcpServers: seedMcpServers,
      toasts: [],

      setSection: (section) =>
        set({ section, paletteOpen: false, modal: null }),

      openSettings: (settingsSection) =>
        set({
          section: "settings",
          settingsSection,
          paletteOpen: false,
          modal: null,
        }),

      setSettingsSection: (settingsSection) => set({ settingsSection }),
      setTheme: (theme) => set({ theme }),
      setAccent: (accent) => set({ accent }),
      setReduceMotion: (reduceMotion) => set({ reduceMotion }),
      toggleContext: () =>
        set((state) => ({ contextCollapsed: !state.contextCollapsed })),
      setPaletteOpen: (paletteOpen) => set({ paletteOpen }),
      openModal: (modal) => set({ modal }),
      closeModal: () => set({ modal: null }),

      newConversation: () => {
        const timestamp = Date.now();
        const conversation: Conversation = {
          id: uid("chat-"),
          title: "New chat",
          messages: [],
          createdAt: timestamp,
          updatedAt: timestamp,
        };
        set((state) => ({
          conversations: [conversation, ...state.conversations],
          activeConversationId: conversation.id,
          section: "chat",
          modal: null,
          paletteOpen: false,
        }));
        return conversation.id;
      },

      deleteConversation: (id) =>
        set((state) => {
          const conversations = state.conversations.filter(
            (conversation) => conversation.id !== id,
          );
          const activeConversationId =
            state.activeConversationId === id
              ? (conversations[0]?.id ?? null)
              : state.activeConversationId;
          return { conversations, activeConversationId };
        }),

      appendMessage: (conversationId, message) =>
        set((state) => ({
          conversations: state.conversations.map((conversation) => {
            if (conversation.id !== conversationId) return conversation;
            const shouldTitle =
              message.role === "user" &&
              (conversation.title === "New chat" ||
                conversation.title.trim() === "");
            const title = shouldTitle
              ? message.content.trim().split("\n")[0]?.slice(0, 52) ||
                "New chat"
              : conversation.title;
            return {
              ...conversation,
              title,
              messages: [...conversation.messages, message],
              updatedAt: Date.now(),
            };
          }),
        })),

      renameConversation: (id, title) =>
        set((state) => ({
          conversations: state.conversations.map((conversation) =>
            conversation.id === id
              ? { ...conversation, title: title.slice(0, 120), updatedAt: Date.now() }
              : conversation,
          ),
        })),

      setActiveConversation: (activeConversationId) =>
        set({ activeConversationId, section: "chat" }),

      addProject: (name, description) => {
        const project = scaffoldProject(name, description);
        set((state) => ({
          projects: [project, ...state.projects],
          activeProjectId: project.id,
          section: "coding",
          modal: null,
        }));
        return project.id;
      },

      deleteProject: (id) =>
        set((state) => {
          const projects = state.projects.filter((project) => project.id !== id);
          const activeProjectId =
            state.activeProjectId === id
              ? (projects[0]?.id ?? null)
              : state.activeProjectId;
          return { projects, activeProjectId };
        }),

      updateProjectFile: (projectId, path, content) =>
        set((state) => ({
          projects: state.projects.map((project) =>
            project.id === projectId
              ? {
                  ...project,
                  updatedAt: Date.now(),
                  files: project.files.map((file) =>
                    file.path === path ? { ...file, content } : file,
                  ),
                }
              : project,
          ),
        })),

      setActiveProject: (activeProjectId) =>
        set({ activeProjectId, section: "coding" }),

      addMcpServer: (server) => {
        const timestamp = Date.now();
        const entry: McpServer = {
          id: uid("mcp-"),
          name: server.name,
          transport: server.transport,
          command: server.command,
          args: server.args,
          url: server.url,
          enabled: true,
          createdAt: timestamp,
        };
        set((state) => ({
          mcpServers: [...state.mcpServers, entry],
          modal: null,
        }));
      },

      updateMcpServer: (id, patch) =>
        set((state) => ({
          mcpServers: state.mcpServers.map((server) =>
            server.id === id ? { ...server, ...patch } : server,
          ),
          modal: null,
        })),

      toggleMcpServer: (id) =>
        set((state) => ({
          mcpServers: state.mcpServers.map((server) =>
            server.id === id
              ? { ...server, enabled: !server.enabled }
              : server,
          ),
        })),

      deleteMcpServer: (id) =>
        set((state) => ({
          mcpServers: state.mcpServers.filter((server) => server.id !== id),
        })),

      addPrompt: (prompt) => {
        const timestamp = Date.now();
        const entry: Prompt = {
          id: uid("prompt-"),
          title: prompt.title,
          description: prompt.description,
          body: prompt.body,
          tags: prompt.tags,
          createdAt: timestamp,
          updatedAt: timestamp,
        };
        set((state) => ({
          prompts: [entry, ...state.prompts],
          modal: null,
        }));
      },

      updatePrompt: (id, patch) =>
        set((state) => ({
          prompts: state.prompts.map((prompt) =>
            prompt.id === id
              ? { ...prompt, ...patch, updatedAt: Date.now() }
              : prompt,
          ),
          modal: null,
        })),

      deletePrompt: (id) =>
        set((state) => ({
          prompts: state.prompts.filter((prompt) => prompt.id !== id),
        })),

      setPromptTag: (promptTag) => set({ promptTag }),

      setOllamaUrl: (ollamaUrl) => set({ ollamaUrl }),
      setModelProvider: (modelProvider) => set({ modelProvider }),
      setModelBaseUrl: (modelBaseUrl) => set({ modelBaseUrl }),
      setModelApiKey: (modelApiKey) => set({ modelApiKey }),
      setModelName: (modelName) => set({ modelName }),
      addProvider: (provider) => {
        const entry: Provider = {
          ...provider,
          id: uid("provider-"),
          createdAt: Date.now(),
        };
        set((state) => ({
          providers: [...state.providers, entry],
        }));
        return entry.id;
      },
      updateProvider: (id, patch) =>
        set((state) => ({
          providers: state.providers.map((provider) =>
            provider.id === id ? { ...provider, ...patch } : provider,
          ),
        })),
      deleteProvider: (id) =>
        set((state) => ({
          providers: state.providers.filter((provider) => provider.id !== id),
          activeProviderId:
            state.activeProviderId === id ? null : state.activeProviderId,
        })),
      setActiveProvider: (activeProviderId) => set({ activeProviderId }),
      updateMessage: (conversationId, messageId, content) =>
        set((state) => ({
          conversations: state.conversations.map((conversation) =>
            conversation.id !== conversationId
              ? conversation
              : {
                  ...conversation,
                  messages: conversation.messages.map((message) =>
                    message.id === messageId ? { ...message, content } : message,
                  ),
                  updatedAt: Date.now(),
                },
          ),
        })),
      setAutoCheckUpdates: (autoCheckUpdates) => set({ autoCheckUpdates }),

      pushToast: (toast) => {
        const id = uid("toast-");
        const entry: Toast = { ...toast, id };
        set((state) => ({ toasts: [...state.toasts, entry] }));
        window.setTimeout(() => get().dismissToast(id), 4000);
      },

      dismissToast: (id) =>
        set((state) => ({
          toasts: state.toasts.filter((toast) => toast.id !== id),
        })),

      resetWorkspace: () =>
        set({
          conversations: [
            { ...welcomeConversation, updatedAt: Date.now() },
          ],
          projects: [gettingStartedProject],
          prompts: seedPrompts,
          mcpServers: seedMcpServers,
          activeConversationId: welcomeConversation.id,
          activeProjectId: gettingStartedProject.id,
          promptTag: null,
          modal: null,
          paletteOpen: false,
          toasts: [],
        }),
    }),
    {
      name: "omnilink.workspace",
      version: 1,
      storage: createJSONStorage(() => zustandStorage),
      partialize: (state) => ({
        section: state.section,
        settingsSection: state.settingsSection,
        theme: state.theme,
        accent: state.accent,
        reduceMotion: state.reduceMotion,
        contextCollapsed: state.contextCollapsed,
        activeConversationId: state.activeConversationId,
        activeProjectId: state.activeProjectId,
        promptTag: state.promptTag,
        ollamaUrl: state.ollamaUrl,
        modelProvider: state.modelProvider,
        modelBaseUrl: state.modelBaseUrl,
        // modelApiKey is stored in the OS keychain / localStorage, never in the DB.
        modelName: state.modelName,
        providers: state.providers,
        activeProviderId: state.activeProviderId,
        autoCheckUpdates: state.autoCheckUpdates,
        conversations: state.conversations,
        projects: state.projects,
        prompts: state.prompts,
        mcpServers: state.mcpServers,
      }),
    },
  ),
);
