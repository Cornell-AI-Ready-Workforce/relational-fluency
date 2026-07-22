import Link from "next/link";

export default function Home() {
  return (
    <main style={{ maxWidth: 640, margin: "4rem auto", padding: "0 1rem" }}>
      <h1>Relational Fluency Study</h1>
      <p>
        Welcome. In this study you will have a short conversation with an AI
        partner in a simulated workplace scenario.
      </p>
      {/* TODO: consent form, Prolific PID capture from URL params */}
      <Link href="/simulation">Begin</Link>
    </main>
  );
}
