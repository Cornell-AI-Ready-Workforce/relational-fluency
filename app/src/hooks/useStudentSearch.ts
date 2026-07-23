import { useState, useEffect, useRef, useCallback, RefObject } from 'react';
import { Student } from '@/lib/types';
import { supabase } from '@/lib/supabase';

export function useStudentSearch(searchField: 'name' | 'net_id', containerRef: RefObject<HTMLDivElement | null>, onSelect: (student: Student) => void) {
  const [results, setResults] = useState<Student[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const search = useCallback(async (query: string) => {
    if (query.trim().length < 1) {
      setResults([]);
      setIsOpen(false);
      return;
    }
    setLoading(true);
    try {
      const { data, error } = await supabase
        .from('students')
        .select('name, net_id')
        .ilike(searchField, `${query}%`)
        .limit(8);
      if (!error && data) {
        setResults(data);
        setIsOpen(data.length > 0);
      }
    } finally {
      setLoading(false);
    }
  }, [searchField]);

  const updateQuery = useCallback((query: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(query), 200);
    setActiveIndex(-1);
  }, [search]);

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [containerRef]);

  const selectResult = useCallback((student: Student) => {
    setIsOpen(false);
    setActiveIndex(-1);
    onSelect(student);
  }, [onSelect]);

  const navigateResults = useCallback((direction: 'up' | 'down') => {
    if (!isOpen) return;
    if (direction === 'down') {
      setActiveIndex(i => Math.min(i + 1, results.length - 1));
    } else {
      setActiveIndex(i => Math.max(i - 1, 0));
    }
  }, [isOpen, results.length]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (!isOpen) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      navigateResults('down');
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      navigateResults('up');
    } else if (e.key === 'Enter' && activeIndex >= 0) {
      e.preventDefault();
      onSelect(results[activeIndex]);
      setIsOpen(false);
      setActiveIndex(-1);
    } else if (e.key === 'Escape') {
      setIsOpen(false);
    }
  }, [isOpen, activeIndex, results, navigateResults, onSelect]);

  return {
    results,
    isOpen,
    loading,
    activeIndex,
    updateQuery,
    handleKeyDown,
    setIsOpen,
    setActiveIndex,
  };
}