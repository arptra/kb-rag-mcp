import { ChildProcess, spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import net from 'node:net';
import path from 'node:path';

export interface MilvusLiteProcessConfig {
    command: string;
    dataDir: string;
    host: string;
    port: number;
    startupTimeoutMs?: number;
}

export function parseMilvusLiteEndpoint(address: string): { host: string; port: number } {
    const url = new URL(address.includes('://') ? address : `tcp://${address}`);
    const port = Number(url.port || '19530');
    if (!Number.isInteger(port) || port <= 0 || port > 65535) {
        throw new Error(`Invalid Milvus Lite port in MILVUS_ADDRESS: ${address}`);
    }
    return { host: url.hostname.replace(/^\[|\]$/g, ''), port };
}

function canConnect(host: string, port: number): Promise<boolean> {
    return new Promise(resolve => {
        const socket = net.createConnection({ host, port });
        const finish = (connected: boolean) => {
            socket.removeAllListeners();
            socket.destroy();
            resolve(connected);
        };
        socket.setTimeout(250);
        socket.once('connect', () => finish(true));
        socket.once('timeout', () => finish(false));
        socket.once('error', () => finish(false));
    });
}

export class MilvusLiteProcess {
    private child?: ChildProcess;
    private owned = false;

    constructor(private readonly config: MilvusLiteProcessConfig) {}

    async ensureStarted(): Promise<void> {
        if (await canConnect(this.config.host, this.config.port)) {
            console.log(`[MILVUS-LITE] Reusing local server at ${this.config.host}:${this.config.port}`);
            return;
        }

        if (!path.isAbsolute(this.config.command) || !existsSync(this.config.command)) {
            throw new Error(`MILVUS_LITE_COMMAND must point to an existing absolute executable: ${this.config.command}`);
        }
        if (!path.isAbsolute(this.config.dataDir)) {
            throw new Error(`MILVUS_LITE_DATA_DIR must be absolute: ${this.config.dataDir}`);
        }

        mkdirSync(this.config.dataDir, { recursive: true });
        console.log(`[MILVUS-LITE] Starting local database at ${this.config.host}:${this.config.port}`);
        console.log(`[MILVUS-LITE] Data directory: ${this.config.dataDir}`);

        this.child = spawn(this.config.command, [
            'server',
            '--data-dir', this.config.dataDir,
            '--host', this.config.host,
            '--port', String(this.config.port),
        ], {
            cwd: this.config.dataDir,
            env: process.env,
            stdio: ['ignore', 'ignore', 'ignore'],
        });
        this.owned = true;

        const deadline = Date.now() + (this.config.startupTimeoutMs ?? 30_000);
        while (Date.now() < deadline) {
            if (this.child.exitCode !== null || this.child.signalCode !== null) {
                throw new Error(`Milvus Lite exited during startup (code=${this.child.exitCode}, signal=${this.child.signalCode})`);
            }
            if (await canConnect(this.config.host, this.config.port)) {
                console.log('[MILVUS-LITE] Local database is ready');
                return;
            }
            await new Promise(resolve => setTimeout(resolve, 200));
        }

        this.stopImmediately();
        throw new Error(`Timed out waiting for Milvus Lite at ${this.config.host}:${this.config.port}`);
    }

    async stop(): Promise<void> {
        if (!this.owned || !this.child || this.child.exitCode !== null || this.child.signalCode !== null) return;

        const child = this.child;
        child.kill('SIGTERM');
        await Promise.race([
            new Promise<void>(resolve => child.once('exit', () => resolve())),
            new Promise<void>(resolve => setTimeout(resolve, 3_000)),
        ]);
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
        this.owned = false;
    }

    stopImmediately(): void {
        if (this.owned && this.child?.exitCode === null && this.child.signalCode === null) this.child.kill('SIGTERM');
        this.owned = false;
    }
}
