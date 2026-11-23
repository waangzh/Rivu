export type ExportFormat = 'csv' | 'json' | 'markdown';

export interface ExportRequest {
  format: ExportFormat;
  filename?: string | null;
  severities: string[];
  paths: string[];
  page: number;
  page_size: number;
}
