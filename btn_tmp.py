import asyncio, json, sys, wave, websockets
PID=sys.argv[1]
async def main():
    url=f"ws://127.0.0.1:8765/ws/participant/voice?scenario=S1A&participant_id={PID}"
    pcm=wave.open("/tmp/answer24.wav","rb").readframes(10**9); log=[]
    async with websockets.connect(url,max_size=None) as ws:
        async def send():
            CH=3200
            for i in range(0,len(pcm),CH): await ws.send(pcm[i:i+CH]); await asyncio.sleep(0.03)
            for _ in range(20): await ws.send(b"\x00"*CH); await asyncio.sleep(0.03)
            await asyncio.sleep(6)
            log.append(">>> clicking 'Talk to Sam'")
            await ws.send(json.dumps({"type":"advance_interaction"}))
        async def recv():
            async for m in ws:
                if isinstance(m,bytes): continue
                e=json.loads(m); t=e.get("type")
                if t=="segment_start":
                    log.append(f"SEGMENT: {e.get('label')} (with {e.get('agent_name')}) | next={(e.get('next') or {}).get('agent_name')}")
                    if 'Sam' in str(e.get('agent_name')): return
                elif t=="error": log.append("ERROR: "+str(e.get('message'))[:90])
        t=asyncio.ensure_future(send())
        try: await asyncio.wait_for(recv(), timeout=75)
        except asyncio.TimeoutError: log.append("(timeout — advance never happened)")
        t.cancel()
    print("\n".join(log))
asyncio.run(main())
