export const exportFormatLabel = (format: string): string => {
  if (format === 'csv') return 'CSV 表格';
  if (format === 'json') return 'JSON 数据';
  if (format === 'markdown') return 'Markdown 报告';
  return format;
};

export const exportFilenameFromDisposition = (header: string | null): string | null => {
  if (!header) return null;
  const match = /filename="([^"]+)"/.exec(header);
  return match ? match[1] : null;
};
