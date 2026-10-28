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
  Asset,
  ChatTemplate,
  CodingProject,
  Conversation,
  McpServer,
  McpTransport,
  Message,
  ModalState,
  ProjectVersion,
  Prompt,
  Provider,
  ModelProvider,
  Section,
  SettingsSection,
  ThemeMode,
  Toast,
  UsageEntry,
  Workspace,
  ToolAuditEntry,
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
    workspaceId: "default",
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
  fontSize: number;
  customCss: string;
  contextCollapsed: boolean;
  focusMode: boolean;
  paletteOpen: boolean;
  modal: ModalState;
  activeConversationId: string | null;
  activeProjectId: string | null;
  workspaces: Workspace[];
  activeWorkspaceId: string | null;
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
  assets: Asset[];
  analytics: Record<string, number>;
  toolAuditLog: ToolAuditEntry[];
  auditLog: { action: string; detail: string; timestamp: number }[];
  usageLog: UsageEntry[];
  costThreshold: number;
  crashReportsEnabled: boolean;
  chatTemplates: ChatTemplate[];
  splitPane: boolean;
  autoComplete: boolean;
  autoCompleteModel: string;
  localOnlyMode: boolean;
  encryptedWorkspace: boolean;
  projectVersions: ProjectVersion[];
  serverUrl: string;
  syncEnabled: boolean;

  setSection: (section: Section) => void;
  openSettings: (section: SettingsSection) => void;
  setSettingsSection: (section: SettingsSection) => void;
  setTheme: (theme: ThemeMode) => void;
  setAccent: (accent: Accent) => void;
  setReduceMotion: (reduce: boolean) => void;
  setFontSize: (size: number) => void;
  setCustomCss: (css: string) => void;
  toggleContext: () => void;
  toggleFocusMode: () => void;
  setPaletteOpen: (open: boolean) => void;
  openModal: (modal: ModalState) => void;
  closeModal: () => void;

  newConversation: () => string;
  deleteConversation: (id: string) => void;
  appendMessage: (conversationId: string, message: Message) => void;
  renameConversation: (id: string, title: string) => void;
  togglePin: (conversationId: string, messageId: string) => void;
  editMessage: (conversationId: string, messageId: string, content: string) => void;
  truncateAfter: (conversationId: string, messageId: string) => void;
  setActiveConversation: (id: string | null) => void;

  addProject: (name: string, description: string) => string;
  deleteProject: (id: string) => void;
  updateProjectFile: (
    projectId: string,
    path: string,
    content: string,
  ) => void;
  setActiveProject: (id: string | null) => void;

  addWorkspace: (name: string) => string;
  deleteWorkspace: (id: string) => void;
  setActiveWorkspace: (id: string | null) => void;
  renameWorkspace: (id: string, name: string) => void;

  addMcpServer: (server: {
    name: string;
    transport: McpTransport;
    command: string;
    args: string;
    url: string;
  }) => void;
  updateMcpServer: (
    id: string,
    patch: Partial<Pick<McpServer, "name" | "command" | "args" | "url" | "transport" | "allowedTools">>,
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

  addAsset: (asset: Omit<Asset, "id" | "createdAt">) => string;
  deleteAsset: (id: string) => void;
  trackEvent: (name: string) => void;
  addAuditEntry: (entry: Omit<ToolAuditEntry, "id" | "timestamp">) => void;
  clearAuditLog: () => void;
  addUsage: (entry: Omit<UsageEntry, "id" | "timestamp">) => void;
  clearUsage: () => void;
  setCostThreshold: (v: number) => void;
  setCrashReportsEnabled: (v: boolean) => void;
  addChatTemplate: (t: { name: string; systemPrompt: string }) => string;
  deleteChatTemplate: (id: string) => void;
  setSplitPane: (v: boolean) => void;
  setAutoComplete: (v: boolean) => void;
  setAutoCompleteModel: (m: string) => void;
  forkConversation: (conversationId: string, messageId: string) => string;
  addAudit: (action: string, detail: string) => void;
  setLocalOnlyMode: (v: boolean) => void;
  setEncryptedWorkspace: (v: boolean) => void;

  createSnapshot: (projectId: string, name: string) => string;
  deleteSnapshot: (id: string) => void;
  restoreSnapshot: (id: string) => void;
  exportWorkspace: () => string;
  importWorkspace: (json: string) => void;
  setServerUrl: (url: string) => void;
  setSyncEnabled: (v: boolean) => void;

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
      fontSize: 13,
      customCss: "",
      contextCollapsed: false,
      focusMode: false,
      paletteOpen: false,
      modal: null,
      activeConversationId: welcomeConversation.id,
      activeProjectId: gettingStartedProject.id,
      workspaces: [{ id: "default", name: "Default", createdAt: Date.now() }],
      activeWorkspaceId: "default",
      promptTag: null,
      ollamaUrl: "http://127.0.0.1:11434",
      modelProvider: "ollama",
      modelBaseUrl: "http://127.0.0.1:11434",
      modelApiKey: "",
      modelName: "auto",
      providers: [],
      activeProviderId: null,
      autoCheckUpdates: true,

      conversations: [welcomeConversation],
      projects: [gettingStartedProject],
      prompts: seedPrompts,
      mcpServers: seedMcpServers,
      toasts: [],
      assets: [],
      analytics: {},
      toolAuditLog: [],
      auditLog: [],
      localOnlyMode: false,
      encryptedWorkspace: false,
      usageLog: [],
      costThreshold: 10,
      crashReportsEnabled: false,
      chatTemplates: [],
      splitPane: false,
      autoComplete: false,
      autoCompleteModel: "",
      projectVersions: [],
      serverUrl: "http://127.0.0.1:8080",
      syncEnabled: false,
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
      setFontSize: (fontSize: number) => set({ fontSize }),
      setCustomCss: (customCss: string) => set({ customCss }),
      toggleContext: () =>
        set((state) => ({ contextCollapsed: !state.contextCollapsed })),
      toggleFocusMode: () =>
        set((state) => ({ focusMode: !state.focusMode })),
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
        get().trackEvent("chat.created");
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

      togglePin: (conversationId, messageId) =>
        set((state) => ({
          conversations: state.conversations.map((conversation) =>
            conversation.id !== conversationId
              ? conversation
              : {
                  ...conversation,
                  messages: conversation.messages.map((message) =>
                    message.id === messageId
                      ? { ...message, pinned: !message.pinned }
                      : message,
                  ),
                },
          ),
        })),

      editMessage: (conversationId, messageId, content) =>
        set((state) => ({
          conversations: state.conversations.map((conversation) =>
            conversation.id !== conversationId
              ? conversation
              : {
                  ...conversation,
                  messages: conversation.messages.map((message) =>
                    message.id === messageId
                      ? { ...message, content }
                      : message,
                  ),
                  updatedAt: Date.now(),
                },
          ),
        })),

      truncateAfter: (conversationId, messageId) =>
        set((state) => ({
          conversations: state.conversations.map((conversation) => {
            if (conversation.id !== conversationId) return conversation;
            const idx = conversation.messages.findIndex((m) => m.id === messageId);
            if (idx === -1) return conversation;
            return {
              ...conversation,
              messages: conversation.messages.slice(0, idx + 1),
              updatedAt: Date.now(),
            };
          }),
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
        get().trackEvent("project.created");
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

      addWorkspace: (name) => {
        const workspace: Workspace = {
          id: uid("workspace-"),
          name: name.slice(0, 60) || "Workspace",
          createdAt: Date.now(),
        };
        set((state) => ({
          workspaces: [...state.workspaces, workspace],
          activeWorkspaceId: workspace.id,
        }));
        return workspace.id;
      },
      deleteWorkspace: (id) =>
        set((state) => {
          if (id === "default") return state;
          const workspaces = state.workspaces.filter((w) => w.id !== id);
          const activeWorkspaceId =
            state.activeWorkspaceId === id ? "default" : state.activeWorkspaceId;
          return { workspaces, activeWorkspaceId };
        }),
      setActiveWorkspace: (activeWorkspaceId) =>
        set({ activeWorkspaceId }),
      renameWorkspace: (id: string, name: string) =>
        set((state) => ({
          workspaces: state.workspaces.map((w) =>
            w.id === id ? { ...w, name: name.slice(0, 60) || w.name } : w,
          ),
        })),

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
          allowedTools: [],
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

      addAsset: (asset) => {
        const entry: Asset = { ...asset, id: uid("asset-"), createdAt: Date.now() };
        set((state) => ({ assets: [entry, ...state.assets] }));
        return entry.id;
      },
      deleteAsset: (id) =>
        set((state) => ({ assets: state.assets.filter((a) => a.id !== id) })),
      trackEvent: (name) =>
        set((state) => ({
          analytics: { ...state.analytics, [name]: (state.analytics[name] ?? 0) + 1 },
        })),

      addAuditEntry: (entry) =>
        set((state) => ({
          toolAuditLog: [
            { ...entry, id: uid("audit-"), timestamp: Date.now() },
            ...state.toolAuditLog,
          ].slice(0, 500),
        })),
      clearAuditLog: () => set({ toolAuditLog: [] }),

      addUsage: (entry) =>
        set((state) => ({
          usageLog: [
            { ...entry, id: uid("usage-"), timestamp: Date.now() },
            ...state.usageLog,
          ].slice(0, 1000),
        })),
      clearUsage: () => set({ usageLog: [] }),
      setCostThreshold: (costThreshold) => set({ costThreshold }),
      setCrashReportsEnabled: (crashReportsEnabled) => set({ crashReportsEnabled }),

      addChatTemplate: (t) => {
        const entry: ChatTemplate = {
          id: uid("tpl-"),
          name: t.name,
          systemPrompt: t.systemPrompt,
          createdAt: Date.now(),
        };
        set((state) => ({ chatTemplates: [...state.chatTemplates, entry] }));
        return entry.id;
      },
      deleteChatTemplate: (id) =>
        set((state) => ({
          chatTemplates: state.chatTemplates.filter((t) => t.id !== id),
        })),
      setSplitPane: (splitPane) => set({ splitPane }),
      setAutoComplete: (autoComplete) => set({ autoComplete }),
      setAutoCompleteModel: (autoCompleteModel) => set({ autoCompleteModel }),
      forkConversation: (conversationId, messageId) => {
        const conv = get().conversations.find((c) => c.id === conversationId);
        if (!conv) return "";
        const idx = conv.messages.findIndex((m) => m.id === messageId);
        if (idx === -1) return "";
        const newConv: Conversation = {
          id: uid("chat-"),
          title: `${conv.title} (fork)`,
          messages: conv.messages.slice(0, idx + 1).map((m) => ({ ...m })),
          createdAt: Date.now(),
          updatedAt: Date.now(),
        };
        set((state) => ({
          conversations: [newConv, ...state.conversations],
          activeConversationId: newConv.id,
        }));
        get().trackEvent("chat.forked");
        return newConv.id;
      },

      addAudit: (action, detail) =>
        set((state) => ({
          auditLog: [
            { action, detail, timestamp: Date.now() },
            ...state.auditLog,
          ].slice(0, 500),
        })),
      setLocalOnlyMode: (localOnlyMode) => set({ localOnlyMode }),
      setEncryptedWorkspace: (encryptedWorkspace) => set({ encryptedWorkspace }),

      createSnapshot: (projectId, name) => {
        const project = get().projects.find((p) => p.id === projectId);
        if (!project) return "";
        const snap: ProjectVersion = {
          id: uid("snap-"),
          projectId,
          name: name.trim() || `Snapshot ${new Date().toLocaleString()}`,
          files: project.files.map((f: { path: string; content: string }) => ({ ...f })),
          createdAt: Date.now(),
        };
        set((state) => ({
          projectVersions: [snap, ...state.projectVersions],
        }));
        get().trackEvent("snapshot.created");
        return snap.id;
      },
      deleteSnapshot: (id) =>
        set((state) => ({
          projectVersions: state.projectVersions.filter((s) => s.id !== id),
        })),
      restoreSnapshot: (id) => {
        const snap = get().projectVersions.find((s) => s.id === id);
        if (!snap) return;
        const project = get().projects.find((p) => p.id === snap.projectId);
        if (!project) return;
        set((state) => ({
          projects: state.projects.map((p) =>
            p.id === snap.projectId
              ? { ...p, files: snap.files.map((f) => ({ ...f })), updatedAt: Date.now() }
              : p,
          ),
        }));
        get().trackEvent("snapshot.restored");
      },
      exportWorkspace: () => {
        const state = get();
        return JSON.stringify(
          {
            version: 1,
            exportedAt: Date.now(),
            projects: state.projects,
            conversations: state.conversations,
            prompts: state.prompts,
            mcpServers: state.mcpServers,
            workspaces: state.workspaces,
          },
          null,
          2,
        );
      },
      importWorkspace: (json) => {
        try {
          const data = JSON.parse(json) as {
            projects?: CodingProject[];
            conversations?: Conversation[];
            prompts?: Prompt[];
            mcpServers?: McpServer[];
            workspaces?: Workspace[];
          };
          set((state) => ({
            projects: data.projects ?? state.projects,
            conversations: data.conversations ?? state.conversations,
            prompts: data.prompts ?? state.prompts,
            mcpServers: data.mcpServers ?? state.mcpServers,
            workspaces: data.workspaces ?? state.workspaces,
          }));
          get().trackEvent("workspace.imported");
        } catch {
          get().pushToast({ title: "Import failed", tone: "danger" });
        }
      },
      setServerUrl: (serverUrl) => set({ serverUrl }),
      setSyncEnabled: (syncEnabled) => set({ syncEnabled }),

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
          workspaces: [{ id: "default", name: "Default", createdAt: Date.now() }],
          activeWorkspaceId: "default",
          modal: null,
          paletteOpen: false,
          toasts: [],
      assets: [],
      analytics: {},
      toolAuditLog: [],
      auditLog: [],
      localOnlyMode: false,
      encryptedWorkspace: false,
      usageLog: [],
      costThreshold: 10,
      crashReportsEnabled: false,
      chatTemplates: [],
      splitPane: false,
      autoComplete: false,
      autoCompleteModel: "",
      projectVersions: [],
      serverUrl: "http://127.0.0.1:8080",
      syncEnabled: false,        }),
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
        fontSize: state.fontSize,
        customCss: state.customCss,
        contextCollapsed: state.contextCollapsed,
        focusMode: state.focusMode,
        activeConversationId: state.activeConversationId,
        activeProjectId: state.activeProjectId,
        workspaces: state.workspaces,
        activeWorkspaceId: state.activeWorkspaceId,
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
        assets: state.assets,
        analytics: state.analytics,
        auditLog: state.auditLog,
        localOnlyMode: state.localOnlyMode,
        encryptedWorkspace: state.encryptedWorkspace,
        usageLog: state.usageLog,
        costThreshold: state.costThreshold,
        crashReportsEnabled: state.crashReportsEnabled,
        chatTemplates: state.chatTemplates,
        splitPane: state.splitPane,
        autoComplete: state.autoComplete,
        autoCompleteModel: state.autoCompleteModel,
        projectVersions: state.projectVersions,
        serverUrl: state.serverUrl,
        syncEnabled: state.syncEnabled,
      }),
    },
  ),
);
