// WebLLM worker: runs the model off the main thread so the tab never freezes.
// Served same-origin at /static/webllm-worker.js and loaded as a module worker.
import { WebWorkerMLCEngineHandler } from "https://esm.run/@mlc-ai/web-llm";
const handler = new WebWorkerMLCEngineHandler();
self.onmessage = (msg) => { handler.onmessage(msg); };
