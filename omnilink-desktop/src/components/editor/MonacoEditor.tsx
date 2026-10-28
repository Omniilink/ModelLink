import Editor, { loader, type OnMount } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import { useRef } from "react";

import cssWorker from "monaco-editor/language/css/css.worker?worker";
import editorWorker from "monaco-editor/editor/editor.worker?worker";
import htmlWorker from "monaco-editor/language/html/html.worker?worker";
import jsonWorker from "monaco-editor/language/json/json.worker?worker";
import tsWorker from "monaco-editor/language/typescript/ts.worker?worker";

type WorkerConstructor = new () => Worker;

const workers: Record<string, WorkerConstructor> = {
  json: jsonWorker,
  css: cssWorker,
  scss: cssWorker,
  less: cssWorker,
  html: htmlWorker,
  handlebars: htmlWorker,
  razor: htmlWorker,
  typescript: tsWorker,
  javascript: tsWorker,
};

const scope = globalThis as unknown as {
  MonacoEnvironment?: {
    getWorker: (moduleId: string, label: string) => Worker;
  };
};

scope.MonacoEnvironment = {
  getWorker(_moduleId: string, label: string) {
    const WorkerClass = workers[label] ?? editorWorker;
    return new WorkerClass();
  },
};

loader.config({ monaco });

const darkTheme: monaco.editor.IStandaloneThemeData = {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "comment", foreground: "70707c", fontStyle: "italic" },
    { token: "keyword", foreground: "c4a7ff" },
    { token: "keyword.control", foreground: "c4a7ff" },
    { token: "string", foreground: "9ae6b4" },
    { token: "number", foreground: "f2c879" },
    { token: "type", foreground: "7dd3fc" },
    { token: "type.identifier", foreground: "7dd3fc" },
    { token: "identifier", foreground: "e8e8ec" },
    { token: "delimiter", foreground: "8e8e9a" },
    { token: "tag", foreground: "f0a6b8" },
    { token: "attribute.name", foreground: "c4a7ff" },
    { token: "attribute.value", foreground: "9ae6b4" },
  ],
  colors: {
    "editor.background": "#0e0e12",
    "editor.foreground": "#e8e8ec",
    "editorLineNumber.foreground": "#42424d",
    "editorLineNumber.activeForeground": "#a0a0ac",
    "editor.selectionBackground": "#8b5cf63d",
    "editor.inactiveSelectionBackground": "#8b5cf622",
    "editor.lineHighlightBackground": "#ffffff06",
    "editorCursor.foreground": "#b9a4ff",
    "editorIndentGuide.background1": "#ffffff0d",
    "editorIndentGuide.activeBackground1": "#ffffff20",
    "editorWidget.background": "#15151a",
    "editorWidget.border": "#2a2a33",
    "editorSuggestWidget.background": "#15151a",
    "editorSuggestWidget.border": "#2a2a33",
    "editorSuggestWidget.selectedBackground": "#26262f",
    "editorHoverWidget.background": "#15151a",
    "editorHoverWidget.border": "#2a2a33",
    "scrollbarSlider.background": "#ffffff14",
    "scrollbarSlider.hoverBackground": "#ffffff24",
    "scrollbarSlider.activeBackground": "#ffffff30",
    "editorOverviewRuler.border": "#00000000",
  },
};

const lightTheme: monaco.editor.IStandaloneThemeData = {
  base: "vs",
  inherit: true,
  rules: [
    { token: "comment", foreground: "8a8a96", fontStyle: "italic" },
    { token: "keyword", foreground: "6d28d9" },
    { token: "string", foreground: "047857" },
    { token: "number", foreground: "b45309" },
    { token: "type", foreground: "0e7490" },
    { token: "identifier", foreground: "1b1b21" },
  ],
  colors: {
    "editor.background": "#ffffff",
    "editor.foreground": "#1b1b21",
    "editorLineNumber.foreground": "#b3b3bd",
    "editorLineNumber.activeForeground": "#60606b",
    "editor.selectionBackground": "#7c3aed2b",
    "editor.lineHighlightBackground": "#00000005",
    "editorCursor.foreground": "#6d28d9",
    "editorIndentGuide.background1": "#0000000d",
    "editorIndentGuide.activeBackground1": "#0000001f",
    "editorWidget.background": "#ffffff",
    "editorWidget.border": "#e4e4e9",
    "editorSuggestWidget.background": "#ffffff",
    "editorSuggestWidget.border": "#e4e4e9",
    "editorSuggestWidget.selectedBackground": "#f0f0f4",
    "scrollbarSlider.background": "#00000014",
    "scrollbarSlider.hoverBackground": "#00000024",
    "editorOverviewRuler.border": "#00000000",
  },
};

export interface CodeEditorProps {
  value: string;
  language: string;
  path: string;
  theme: "dark" | "light";
  onChange: (value: string) => void;
  onSave: () => void;
}

export default function MonacoEditor({
  value,
  language,
  path,
  theme,
  onChange,
  onSave,
}: CodeEditorProps) {
  const saveRef = useRef(onSave);
  saveRef.current = onSave;

  const beforeMount = (instance: typeof monaco) => {
    instance.editor.defineTheme("omnilink-dark", darkTheme);
    instance.editor.defineTheme("omnilink-light", lightTheme);
  };

  const onMount: OnMount = (editor, instance) => {
    editor.addCommand(instance.KeyMod.CtrlCmd | instance.KeyCode.KeyS, () => {
      saveRef.current();
    });
    editor.focus();
  };

  return (
    <Editor
      value={value}
      language={language}
      path={path}
      theme={theme === "dark" ? "omnilink-dark" : "omnilink-light"}
      beforeMount={beforeMount}
      onMount={onMount}
      onChange={(next) => onChange(next ?? "")}
      loading={<div className="h-full w-full bg-[var(--bg-surface)]" />}
      options={{
        fontFamily:
          '"JetBrains Mono Variable", "JetBrains Mono", ui-monospace, monospace',
        fontSize: 12.5,
        lineHeight: 21,
        fontLigatures: true,
        minimap: { enabled: true, scale: 1, showSlider: "mouseover" },
        scrollBeyondLastLine: false,
        smoothScrolling: true,
        cursorBlinking: "smooth",
        cursorSmoothCaretAnimation: "on",
        renderLineHighlight: "line",
        renderWhitespace: "selection",
        overviewRulerBorder: false,
        hideCursorInOverviewRuler: true,
        padding: { top: 14, bottom: 24 },
        automaticLayout: true,
        tabSize: 2,
        wordWrap: "off",
        scrollbar: {
          verticalScrollbarSize: 10,
          horizontalScrollbarSize: 10,
          useShadows: false,
        },
        suggest: { showWords: false },
        quickSuggestions: { other: true, comments: false, strings: false },
        fixedOverflowWidgets: true,
      }}
    />
  );
}
