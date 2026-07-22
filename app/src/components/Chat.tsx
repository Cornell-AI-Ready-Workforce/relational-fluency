"use client";

import { useState } from "react";
import { sendTurn, type Message } from "@/lib/api";

export default function Chat({ scenarioId }: { scenarioId: string }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleSend() {
    if (!input.trim() || busy) return;
    const next: Message[] = [...messages, { role: "user", content: input }];
    setMessages(next);
    setInput("");
    setBusy(true);
    try {
      const reply = await sendTurn(scenarioId, next);
      setMessages([...next, reply]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div
        style={{
          border: "1px solid #ddd",
          borderRadius: 8,
          minHeight: 320,
          padding: 12,
          marginBottom: 12,
        }}
      >
        {messages.map((m, i) => (
          <p key={i}>
            <strong>{m.role === "user" ? "You" : "Partner"}:</strong>{" "}
            {m.content}
          </p>
        ))}
        {busy && <p><em>Partner is typing…</em></p>}
      </div>
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && handleSend()}
        placeholder="Type your message"
        style={{ width: "75%", padding: 8 }}
      />
      <button onClick={handleSend} disabled={busy} style={{ padding: 8, marginLeft: 8 }}>
        Send
      </button>
    </div>
  );
}
