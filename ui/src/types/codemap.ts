// codemap 领域类型(CodeMap/CodeViewer 组件契约;ws/HTTP 传输层复用)
export interface CodemapCitation {
  file_path: string;
  start_line: number | null;
  end_line: number | null;
  snippet: string;
}

export interface CodemapStep {
  id: string;
  label: string;
  code: string;
  citation: CodemapCitation | null;
}

export interface CodemapSection {
  id: string;
  title: string;
  guide: string;
  diagram: string;
  steps: CodemapStep[];
}

export interface CodemapData {
  title: string;
  summary: string;
  sections: CodemapSection[];
}

export type CodemapPhase = 'analyzing' | 'initial_codemap' | 'diagrams';

// One NDJSON event streamed from the backend.
export type CodemapEvent =
  | { type: 'phase'; phase: CodemapPhase; status: 'start' | 'done'; [k: string]: unknown }
  | { type: 'codemap'; data: CodemapData }
  | { type: 'error'; message: string; stage?: string }
  | { type: 'done' };
