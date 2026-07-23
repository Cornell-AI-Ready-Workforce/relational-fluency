let _audioBlob: Blob | null = null;
let _durationSeconds = 0;

export function setAudioBlob(blob: Blob, duration: number): void {
  _audioBlob = blob;
  _durationSeconds = duration;
}

export function getAudioBlob(): { blob: Blob | null; duration: number } {
  return { blob: _audioBlob, duration: _durationSeconds };
}

export function clearAudioBlob(): void {
  _audioBlob = null;
  _durationSeconds = 0;
}
