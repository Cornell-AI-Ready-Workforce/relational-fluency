import { NextRequest, NextResponse } from 'next/server';
import { InterviewSession, InterviewReport, ScoreDimension } from '@/lib/types';

// LLM judge served through the same LiteLLM gateway as the interviewer
const BASE_URL = (process.env.OPENAI_BASE_URL ?? 'https://api.openai.com').replace(/\/$/, '');
const JUDGE_MODEL = process.env.LITELLM_JUDGE_MODEL ?? 'nto.gemini-2.5-flash';

export async function POST(request: NextRequest) {
  try {
    const session: InterviewSession = await request.json();

    if (!session || !session.messages) {
      return NextResponse.json({ error: 'Missing session data' }, { status: 400 });
    }

    // Build the transcript text
    const transcriptText = session.messages
      .filter(m => m.content.trim())
      .map(m => {
        const role = m.role === 'interviewer' ? 'Interviewer' : 'Candidate';
        const cleanContent = m.content.replace(/\{"action":\s*"[^"]*"\}/g, '').trim();
        return `${role}: ${cleanContent}`;
      })
      .join('\n\n');

    const selfReportText = session.selfReport
      ? `
The candidate also provided these self-ratings (1-5 scale):
- Communication: ${session.selfReport.communication}/5
- Collaboration: ${session.selfReport.collaboration}/5
- Conflict Resolution: ${session.selfReport.conflictResolution}/5
- Adaptability: ${session.selfReport.adaptability}/5
- Personal reflection: "${session.selfReport.reflection}"
`
      : '';

    const scoringPrompt = `You are an expert soft skills assessor acting as an LLM judge. Analyze the following interview transcript and provide rigorous, evidence-based scores with full reasoning chains.

Interview format: ${session.format === 'star' ? 'STAR Behavioral Interview' : 'Role Play Scenarios'}
Candidate name: ${session.candidateName}
${selfReportText}

TRANSCRIPT:
${transcriptText}

Evaluate the candidate on these 4 dimensions (score 1-5):
1. Communication (clarity, active listening, ability to articulate thoughts)
2. Collaboration (willingness to work with others, sharing credit, team mindset)
3. Conflict Resolution (handling disagreements constructively, finding solutions)
4. Adaptability (flexibility, openness to different approaches, resilience)

Scoring guide:
- 5: Exceptional - multiple strong examples, sophisticated understanding
- 4: Strong - clear examples, good self-awareness
- 3: Developing - some evidence but inconsistent or vague
- 2: Limited - weak examples or concerning patterns
- 1: Insufficient - no meaningful evidence or problematic responses

For each dimension, you MUST first write out your step-by-step reasoning process (what you observed, what it implies, how you weighed the evidence) BEFORE assigning a score. This reasoning chain is required for transparency and auditability.

Respond ONLY with a valid JSON object in exactly this format:
{
  "scores": {
    "communication": {
      "reasoning": "<step-by-step chain-of-thought: what specific moments you observed, how you interpreted them, what score criteria they meet, and why you landed on this score>",
      "score": <number 1-5>,
      "evidence": "<2-3 sentence specific evidence from the transcript>"
    },
    "collaboration": {
      "reasoning": "<step-by-step chain-of-thought>",
      "score": <number 1-5>,
      "evidence": "<2-3 sentence specific evidence from the transcript>"
    },
    "conflictResolution": {
      "reasoning": "<step-by-step chain-of-thought>",
      "score": <number 1-5>,
      "evidence": "<2-3 sentence specific evidence from the transcript>"
    },
    "adaptability": {
      "reasoning": "<step-by-step chain-of-thought>",
      "score": <number 1-5>,
      "evidence": "<2-3 sentence specific evidence from the transcript>"
    }
  },
  "overallScore": <number 1-5, weighted average>,
  "summary": "<3-4 sentence narrative summary of the candidate's teamwork and collaboration profile>",
  "strengths": [
    "<specific strength with example>",
    "<specific strength with example>",
    "<specific strength with example>"
  ],
  "areasForGrowth": [
    "<specific area with suggestion>",
    "<specific area with suggestion>",
    "<specific area with suggestion>"
  ]
}`;

    const response = await fetch(`${BASE_URL}/v1/chat/completions`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: JUDGE_MODEL,
        max_tokens: 4096,
        messages: [{ role: 'user', content: scoringPrompt }],
      }),
    });

    if (!response.ok) {
      const err = await response.text();
      console.error('Judge model error:', err.slice(0, 500));
      return NextResponse.json({ error: 'Judge model request failed' }, { status: 502 });
    }

    const completion = await response.json();
    const rawText: string | undefined = completion.choices?.[0]?.message?.content;
    if (!rawText) {
      return NextResponse.json({ error: 'Unexpected response format' }, { status: 500 });
    }

    // Extract JSON from the response
    const jsonMatch = rawText.match(/\{[\s\S]*\}/);
    if (!jsonMatch) {
      return NextResponse.json({ error: 'Could not parse scoring response' }, { status: 500 });
    }

    const parsed = JSON.parse(jsonMatch[0]);

    const dimensionMap: { name: string; key: keyof typeof parsed.scores }[] = [
      { name: 'Communication', key: 'communication' },
      { name: 'Collaboration', key: 'collaboration' },
      { name: 'Conflict Resolution', key: 'conflictResolution' },
      { name: 'Adaptability', key: 'adaptability' },
    ];

    const scores: ScoreDimension[] = dimensionMap.map(({ name, key }) => ({
      name,
      key: key as ScoreDimension['key'],
      score: parsed.scores[key].score,
      evidence: parsed.scores[key].evidence,
      reasoning: parsed.scores[key].reasoning ?? '',
    }));

    const report: InterviewReport = {
      candidateName: session.candidateName,
      format: session.format,
      scores,
      overallScore: parsed.overallScore,
      summary: parsed.summary,
      strengths: parsed.strengths,
      areasForGrowth: parsed.areasForGrowth,
      transcript: session.messages,
      selfReport: session.selfReport,
      judgeModel: completion.model ?? JUDGE_MODEL,
    };

    return NextResponse.json(report);
  } catch (error) {
    console.error('Report API error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
