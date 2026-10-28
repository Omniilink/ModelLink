// Minimal Chrome DevTools Protocol client (raw WebSocket over node:net).
// Node 20 has no global WebSocket, and adding a dependency for a test harness
// is not worth it: CDP only needs masked text frames.
import crypto from "node:crypto";
import net from "node:net";

function encodeFrame(opcode, payload) {
  const data = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  const mask = crypto.randomBytes(4);
  let header;

  if (data.length < 126) {
    header = Buffer.from([0x80 | opcode, 0x80 | data.length]);
  } else if (data.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 0x80 | 126;
    header.writeUInt16BE(data.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 0x80 | 127;
    header.writeBigUInt64BE(BigInt(data.length), 2);
  }

  const masked = Buffer.from(data);
  for (let i = 0; i < masked.length; i += 1) {
    masked[i] ^= mask[i % 4];
  }
  return Buffer.concat([header, mask, masked]);
}

export function connectCDP(wsUrl) {
  const url = new URL(wsUrl);

  return new Promise((resolve, reject) => {
    const socket = net.connect(
      { host: url.hostname, port: Number(url.port) },
      () => {
        const key = crypto.randomBytes(16).toString("base64");
        socket.write(
          `GET ${url.pathname}${url.search} HTTP/1.1\r\n` +
            `Host: ${url.host}\r\n` +
            `Upgrade: websocket\r\n` +
            `Connection: Upgrade\r\n` +
            `Sec-WebSocket-Key: ${key}\r\n` +
            `Sec-WebSocket-Version: 13\r\n\r\n`,
        );
      },
    );

    let buffer = Buffer.alloc(0);
    let handshaken = false;
    let nextId = 1;
    const pending = new Map();
    const eventHandlers = new Set();
    let fragOpcode = 0;
    let fragParts = [];

    const deliver = (opcode, payload) => {
      if (opcode !== 0x1) return;
      let message;
      try {
        message = JSON.parse(payload.toString("utf8"));
      } catch {
        return;
      }
      if (message.id && pending.has(message.id)) {
        const { resolve: done, reject: fail } = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) fail(new Error(message.error.message ?? "CDP error"));
        else done(message.result);
        return;
      }
      for (const handler of eventHandlers) handler(message);
    };

    const onData = (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);

      if (!handshaken) {
        const idx = buffer.indexOf("\r\n\r\n");
        if (idx === -1) return;
        const head = buffer.slice(0, idx).toString("utf8");
        buffer = buffer.slice(idx + 4);
        if (!/^HTTP\/1\.1 101/.test(head)) {
          reject(new Error(`WebSocket handshake failed: ${head.split("\r\n")[0]}`));
          socket.destroy();
          return;
        }
        handshaken = true;
        resolve(api);
      }

      for (;;) {
        if (buffer.length < 2) return;
        const b0 = buffer[0];
        const b1 = buffer[1];
        const fin = (b0 & 0x80) !== 0;
        const opcode = b0 & 0x0f;
        const masked = (b1 & 0x80) !== 0;
        let length = b1 & 0x7f;
        let offset = 2;

        if (length === 126) {
          if (buffer.length < 4) return;
          length = buffer.readUInt16BE(2);
          offset = 4;
        } else if (length === 127) {
          if (buffer.length < 10) return;
          length = Number(buffer.readBigUInt64BE(2));
          offset = 10;
        }

        let mask;
        if (masked) {
          if (buffer.length < offset + 4) return;
          mask = buffer.slice(offset, offset + 4);
          offset += 4;
        }
        if (buffer.length < offset + length) return;

        let payload = buffer.slice(offset, offset + length);
        buffer = buffer.slice(offset + length);

        if (mask) {
          payload = Buffer.from(payload);
          for (let i = 0; i < payload.length; i += 1) {
            payload[i] ^= mask[i % 4];
          }
        }

        if (opcode === 0x8) {
          socket.end();
          return;
        }
        if (opcode === 0x9) {
          socket.write(encodeFrame(0xa, payload));
          continue;
        }
        if (opcode === 0xa) continue;

        if (opcode === 0x0) {
          fragParts.push(payload);
          if (fin) {
            deliver(fragOpcode, Buffer.concat(fragParts));
            fragParts = [];
          }
        } else if (opcode === 0x1 || opcode === 0x2) {
          if (fin) deliver(opcode, payload);
          else {
            fragOpcode = opcode;
            fragParts = [payload];
          }
        }
      }
    };

    socket.on("data", onData);
    socket.on("error", reject);
    socket.on("close", () => {
      for (const { reject: fail } of pending.values()) {
        fail(new Error("CDP socket closed"));
      }
      pending.clear();
    });

    const api = {
      send(method, params = {}) {
        const id = nextId;
        nextId += 1;
        return new Promise((done, fail) => {
          pending.set(id, { resolve: done, reject: fail });
          socket.write(encodeFrame(0x1, JSON.stringify({ id, method, params })));
        });
      },
      onEvent(handler) {
        eventHandlers.add(handler);
        return () => eventHandlers.delete(handler);
      },
      close() {
        try {
          socket.write(encodeFrame(0x8, Buffer.alloc(0)));
        } catch {
          // Already gone.
        }
        socket.end();
      },
    };
  });
}

/** Wait for the WebView2 devtools endpoint and return the app page target. */
export async function waitForTarget({
  port = 9222,
  timeoutMs = 30000,
  predicate = () => true,
} = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "no target found";

  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const match = targets.find(
        (target) =>
          target.type === "page" && target.webSocketDebuggerUrl && predicate(target),
      );
      if (match) return match;
      lastError = `targets: ${targets.map((t) => `${t.type}:${t.url}`).join(", ")}`;
    } catch (error) {
      lastError = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }

  throw new Error(`DevTools target not available on :${port} (${lastError})`);
}
