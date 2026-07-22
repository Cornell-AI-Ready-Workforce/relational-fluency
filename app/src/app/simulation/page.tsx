import Chat from "@/components/Chat";

export default function SimulationPage() {
  return (
    <main style={{ maxWidth: 720, margin: "2rem auto", padding: "0 1rem" }}>
      <h2>Simulation encounter</h2>
      <Chat scenarioId="placeholder-scenario" />
    </main>
  );
}
