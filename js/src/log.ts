// Where mendwright's diagnostic lines go. As in the Python package, a WARNING (an applied
// heal, a declined one, a handled interruption) shows on stderr, prefixed with
// 'mendwright: ', and an INFO line (a skipped step) is left out unless a sink asks for it.

export type LogLevel = 'debug' | 'info' | 'warning';
export type LogSink = (level: LogLevel, message: string) => void;

export const defaultSink: LogSink = (level, message) => {
  if (level === 'warning') console.warn('mendwright: ' + message);
};

let sink: LogSink = defaultSink;

/** Send mendwright's lines to sink (null restores the default). Tests use it. */
export function setLogSink(next: LogSink | null): void {
  sink = next ?? defaultSink;
}

export function log(level: LogLevel, message: string): void {
  try {
    sink(level, message);
  } catch {
    // a broken sink must not break a heal
  }
}
