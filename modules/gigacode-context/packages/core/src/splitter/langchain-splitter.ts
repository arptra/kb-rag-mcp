import { Splitter, CodeChunk } from './index';

/**
 * Dependency-free recursive character splitter kept under the original class
 * name for API and snapshot compatibility.
 */
export class LangChainCodeSplitter implements Splitter {
    private chunkSize: number = 1000;
    private chunkOverlap: number = 200;

    constructor(chunkSize?: number, chunkOverlap?: number) {
        if (chunkSize) this.setChunkSize(chunkSize);
        if (chunkOverlap !== undefined) this.setChunkOverlap(chunkOverlap);
    }

    async split(code: string, language: string, filePath?: string): Promise<CodeChunk[]> {
        if (!code) return [];

        const chunks: CodeChunk[] = [];
        const lineStarts = this.buildLineStarts(code);
        let start = 0;

        while (start < code.length) {
            const hardEnd = Math.min(start + this.chunkSize, code.length);
            const end = hardEnd < code.length
                ? this.findNaturalBoundary(code, start, hardEnd)
                : hardEnd;
            const content = code.slice(start, end);

            chunks.push({
                content,
                metadata: {
                    startLine: this.lineAtOffset(lineStarts, start),
                    endLine: this.lineAtOffset(lineStarts, Math.max(start, end - 1)),
                    language,
                    filePath,
                },
            });

            if (end >= code.length) break;
            const nextStart = Math.max(start + 1, end - this.chunkOverlap);
            start = this.advancePastLeadingWhitespace(code, nextStart, end);
        }

        return chunks;
    }

    setChunkSize(chunkSize: number): void {
        if (!Number.isInteger(chunkSize) || chunkSize <= 0) {
            throw new Error('chunkSize must be a positive integer');
        }
        this.chunkSize = chunkSize;
        if (this.chunkOverlap >= chunkSize) {
            this.chunkOverlap = Math.max(0, chunkSize - 1);
        }
    }

    setChunkOverlap(chunkOverlap: number): void {
        if (!Number.isInteger(chunkOverlap) || chunkOverlap < 0) {
            throw new Error('chunkOverlap must be a non-negative integer');
        }
        if (chunkOverlap >= this.chunkSize) {
            throw new Error('chunkOverlap must be smaller than chunkSize');
        }
        this.chunkOverlap = chunkOverlap;
    }

    private findNaturalBoundary(code: string, start: number, hardEnd: number): number {
        const minimumBoundary = start + Math.floor(this.chunkSize * 0.5);
        const window = code.slice(start, hardEnd);
        const separators = ['\n\n', '\n', '; ', ', ', ' '];

        for (const separator of separators) {
            const relative = window.lastIndexOf(separator);
            if (relative >= 0) {
                const candidate = start + relative + separator.length;
                if (candidate >= minimumBoundary) return candidate;
            }
        }

        return hardEnd;
    }

    private advancePastLeadingWhitespace(code: string, start: number, upperBound: number): number {
        let offset = start;
        while (offset < upperBound && (code[offset] === ' ' || code[offset] === '\t')) {
            offset += 1;
        }
        return offset;
    }

    private buildLineStarts(code: string): number[] {
        const starts = [0];
        for (let index = 0; index < code.length; index += 1) {
            if (code[index] === '\n') starts.push(index + 1);
        }
        return starts;
    }

    private lineAtOffset(lineStarts: number[], offset: number): number {
        let low = 0;
        let high = lineStarts.length - 1;
        while (low <= high) {
            const middle = Math.floor((low + high) / 2);
            if (lineStarts[middle] <= offset) low = middle + 1;
            else high = middle - 1;
        }
        return high + 1;
    }
}
