import { useRef } from 'react';
import { Student } from '@/lib/types';
import { useStudentSearch } from '@/hooks/useStudentSearch';

interface AutocompleteInputProps {
  label: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  onSelect: (student: Student) => void;
  searchField: 'name' | 'net_id';
  onKeyDown?: (e: React.KeyboardEvent) => void;
}

export default function AutocompleteInput({
  label,
  placeholder,
  value,
  onChange,
  onSelect,
  searchField,
  onKeyDown,
}: AutocompleteInputProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { results, isOpen, loading, activeIndex, updateQuery, handleKeyDown, setIsOpen, setActiveIndex } = useStudentSearch(searchField, containerRef, onSelect);

  const handleInputChange = (newValue: string) => {
    onChange(newValue);
    updateQuery(newValue);
  };

  const handleFocus = () => {
    if (results.length > 0) setIsOpen(true);
  };

  const handleKeyDownWrapper = (e: React.KeyboardEvent<HTMLInputElement>) => {
    handleKeyDown(e);
    onKeyDown?.(e);
  };

  return (
    <div ref={containerRef} className="relative">
      <label className="block text-sm font-medium text-gray-700 mb-2">{label}</label>
      <div className="relative">
        <input
          type="text"
          value={value}
          onChange={e => handleInputChange(e.target.value)}
          onFocus={handleFocus}
          onKeyDown={handleKeyDownWrapper}
          placeholder={placeholder}
          autoComplete="off"
          className="w-full px-4 py-3 rounded-xl border border-gray-200 bg-white text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-purple-500 focus:border-transparent transition-all pr-10"
        />
        {loading && (
          <div className="absolute right-3 top-1/2 -translate-y-1/2">
            <svg className="w-4 h-4 text-gray-400 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
          </div>
        )}
      </div>

      {isOpen && results.length > 0 && (
        <ul className="absolute z-50 mt-1 w-full bg-white border border-gray-200 rounded-xl shadow-lg overflow-hidden">
          {results.map((student, i) => (
            <li
              key={student.net_id}
              onMouseDown={() => onSelect(student)}
              onMouseEnter={() => setActiveIndex(i)}
              className={`flex items-center justify-between px-4 py-2.5 cursor-pointer text-sm transition-colors ${
                i === activeIndex ? 'bg-purple-50 text-purple-900' : 'text-gray-800 hover:bg-gray-50'
              }`}
            >
              <span className="font-medium">{student.name}</span>
              <span className="text-xs text-gray-400 font-mono">{student.net_id}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}