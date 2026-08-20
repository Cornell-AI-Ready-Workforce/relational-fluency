import asyncio, json, sys, wave, websockets
PID, K = sys.argv[1], sys.argv[2]
async def main():
    url=(f"wss://rf.ai-ready-workforce.ai.cornell.edu/ws/participant/voice"
         f"?scenario=S1A&participant_id={PID}&key={K}")
    pcm=wave.open("/tmp/answer24.wav","rb").readframes(10**9)
    seen=[]; agent=""; user=""
    async with websockets.connect(url,max_size=None) as ws:
        async def send():
            CH=3200
            for i in range(0,len(pcm),CH): await ws.send(pcm[i:i+CH]); await asyncio.sleep(0.03)
            for _ in range(30): await ws.send(b"\x00"*CH); await asyncio.sleep(0.03)
        async def recv():
            nonlocal agent,user
            async for m in ws:
                if isinstance(m,bytes):
                    if "AUDIO" not in seen: seen.append("AUDIO")
                    continue
                e=json.loads(m); t=e.get("type")
                if t not in seen: seen.append(t)
                if t=="assistant_text_delta": agent+=e.get("text","")
                if t=="user_transcript": user=e.get("text","")
                if t=="error": print("SERVER ERROR:", str(e.get("message"))[:160])
                if t=="assistant_done": return
        t=asyncio.ensure_future(send())
        try: await asyncio.wait_for(recv(), timeout=90)
        except asyncio.TimeoutError: print("(timeout)")
        t.cancel()
    print("events:", seen)
    print("heard :", user)
    print("agent :", agent[:110])
asyncio.run(main())
