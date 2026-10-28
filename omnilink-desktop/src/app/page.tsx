"use client";
import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";

const tabs = ["Chat", "Coding", "Agents", "Settings"] as const;
type Tab = typeof tabs[number];

export default function Home() {
  const [tab, setTab] = useState<Tab>("Chat");
  return (
    <main className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="flex h-screen">
        <aside className="w-56 border-r border-zinc-800 p-4 flex flex-col gap-2">
          <div className="text-lg font-semibold mb-4">OmniLink</div>
          {tabs.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`text-left px-3 py-2 rounded-lg transition ${tab === t ? "bg-zinc-800" : "hover:bg-zinc-900"}`}
            >
              {t}
            </button>
          ))}
        </aside>
        <section className="flex-1 p-6 overflow-auto">
          <AnimatePresence mode="wait">
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.18 }}
            >
              <h1 className="text-2xl font-semibold mb-4">{tab}</h1>
              {tab === "Chat" && <ChatView />}
              {tab === "Coding" && <Placeholder text="Agentic coding workspace coming next." />}
              {tab === "Agents" && <Placeholder text="MCP agents and servers." />}
              {tab === "Settings" && <Placeholder text="Preferences and updates." />}
            </motion.div>
          </AnimatePresence>
        </section>
      </div>
    </main>
  );
}

function Placeholder({ text }: { text: string }) {
  return <div className="text-zinc-400">{text}</div>;
}

function ChatView() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<{ role: string; text: string }[]>([]);
  const send = () => {
    if (!input.trim()) return;
    setMessages((m) => [...m, { role: "user", text: input }]);
    setInput("");
    setTimeout(() => {
      setMessages((m) => [...m, { role: "assistant", text: "Echo: " + input }]);
    }, 200);
  };
  return (
    <div className="flex flex-col gap-3 max-w-3xl">
      <div className="flex flex-col gap-2 min-h-[300px]">
        {messages.map((m, i) => (
          <div key={i} className={`px-3 py-2 rounded-lg ${m.role === "user" ? "bg-zinc-800 self-end" : "bg-zinc-900"}`}>
            {m.text}
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          className="flex-1 bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2"
          placeholder="Message OmniLink..."
        />
        <button onClick={send} className="px-4 py-2 bg-indigo-600 rounded-lg hover:bg-indigo-500">
          Send
        </button>
      </div>
    </div>
  );
}
