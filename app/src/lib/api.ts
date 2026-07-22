export type Message = { role: "user" | "assistant"; content: string };

const BASE = process.env.NEXT_PUBLIC_AGENT_API_URL ?? "http://localhost:8000";

export async function sendTurn(
  scenarioId: string,
  messages: Message[]
): Promise<Message> {
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario_id: scenarioId, messages }),
  });
  if (!res.ok) throw new Error(`Agent API error: ${res.status}`);
  const data = await res.json();
  return { role: "assistant", content: data.reply };
}
