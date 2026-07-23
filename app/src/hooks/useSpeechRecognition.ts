'use client';

import { useState, useEffect, useRef, useCallback } from 'react';

interface ISpeechRecognitionEvent extends Event {
  results: SpeechRecognitionResultList;
  resultIndex: number;
}

interface ISpeechRecognitionErrorEvent extends Event {
  error: string;
}

interface ISpeechRecognition extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  onresult: ((event: ISpeechRecognitionEvent) => void) | null;
  onerror: ((event: ISpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
}

interface ISpeechRecognitionConstructor {
  new (): ISpeechRecognition;
}

interface UseSpeechRecognitionReturn {
  transcript: string;        // accumulated final transcript
  interimTranscript: string; // live in-progress text
  isListening: boolean;
  startListening: () => void;
  stopListening: () => void;
  error: string | null;
  isSupported: boolean;
}

export function useSpeechRecognition(): UseSpeechRecognitionReturn {
  const [transcript, setTranscript] = useState('');
  const [interimTranscript, setInterimTranscript] = useState('');
  const [isListening, setIsListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isSupported, setIsSupported] = useState(false);

  const recognitionRef = useRef<ISpeechRecognition | null>(null);
  const accumulatedRef = useRef(''); // accumulates final segments across pauses
  const shouldRestartRef = useRef(false); // restart if stopped unexpectedly mid-session

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const w = window as Window & {
      SpeechRecognition?: ISpeechRecognitionConstructor;
      webkitSpeechRecognition?: ISpeechRecognitionConstructor;
    };

    const SpeechRecognitionAPI = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!SpeechRecognitionAPI) return;

    setIsSupported(true);

    const recognition = new SpeechRecognitionAPI();
    recognition.continuous = true;      // keep listening through natural pauses
    recognition.interimResults = true;  // show live text as user speaks
    recognition.lang = 'en-US';

    recognition.onresult = (event: ISpeechRecognitionEvent) => {
      let interim = '';
      let newFinals = '';

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          newFinals += result[0].transcript + ' ';
        } else {
          interim += result[0].transcript;
        }
      }

      if (newFinals) {
        accumulatedRef.current += newFinals;
        setTranscript(accumulatedRef.current.trim());
      }
      setInterimTranscript(interim);
    };

    recognition.onerror = (event: ISpeechRecognitionErrorEvent) => {
      if (event.error !== 'no-speech' && event.error !== 'aborted') {
        setError(`Speech recognition error: ${event.error}`);
        setIsListening(false);
        shouldRestartRef.current = false;
      }
    };

    recognition.onend = () => {
      // Chrome's continuous mode can stop unexpectedly — restart if still meant to be listening
      if (shouldRestartRef.current) {
        try {
          recognition.start();
        } catch {
          setIsListening(false);
          shouldRestartRef.current = false;
        }
      } else {
        setIsListening(false);
        setInterimTranscript('');
      }
    };

    recognitionRef.current = recognition;
  }, []);

  const startListening = useCallback(() => {
    if (!recognitionRef.current) return;
    accumulatedRef.current = '';
    setTranscript('');
    setInterimTranscript('');
    setError(null);
    shouldRestartRef.current = true;
    try {
      recognitionRef.current.start();
      setIsListening(true);
    } catch {
      setError('Could not start speech recognition. Please try again.');
      shouldRestartRef.current = false;
    }
  }, []);

  const stopListening = useCallback(() => {
    if (!recognitionRef.current) return;
    shouldRestartRef.current = false;
    recognitionRef.current.stop();
    setIsListening(false);
    setInterimTranscript('');
  }, []);

  return { transcript, interimTranscript, isListening, startListening, stopListening, error, isSupported };
}
