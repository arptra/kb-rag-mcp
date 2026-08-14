import * as net from 'node:net';

let installed = false;

function normalizeHost(host: string): string {
    return host.trim().replace(/^\[|\]$/g, '').toLowerCase();
}

export function isLoopbackHost(host: string): boolean {
    const normalized = normalizeHost(host);
    return normalized === 'localhost'
        || normalized === '::1'
        || normalized.startsWith('127.')
        || normalized.startsWith('::ffff:127.');
}

export function assertLoopbackUrl(input: string | URL): void {
    const url = input instanceof URL ? input : new URL(input);
    if (url.protocol === 'file:' || url.protocol === 'data:') {
        return;
    }
    if (!isLoopbackHost(url.hostname)) {
        throw new Error(`Offline network policy blocked outbound URL: ${url.origin}`);
    }
}

function socketHost(args: unknown[]): string | undefined {
    const first = args[0];
    if (typeof first === 'number') {
        return typeof args[1] === 'string' ? args[1] : 'localhost';
    }
    if (typeof first === 'object' && first !== null) {
        const options = first as { host?: unknown; path?: unknown };
        if (typeof options.path === 'string') {
            return undefined;
        }
        return typeof options.host === 'string' ? options.host : 'localhost';
    }
    // A string first argument is a local IPC socket path.
    return undefined;
}

/**
 * Defense in depth for the closed-contour MCP process. The configured local
 * providers are already fail-closed; this guard also blocks accidental direct
 * TCP/fetch calls introduced by dependencies or future changes.
 */
export function installLoopbackOnlyNetworkGuard(): void {
    if (installed) {
        return;
    }
    installed = true;

    const socketPrototype = net.Socket.prototype as unknown as {
        connect: (...args: unknown[]) => net.Socket;
    };
    const originalConnect = socketPrototype.connect;
    socketPrototype.connect = function guardedConnect(this: net.Socket, ...args: unknown[]): net.Socket {
        const host = socketHost(args);
        if (host && !isLoopbackHost(host)) {
            throw new Error(`Offline network policy blocked outbound TCP host: ${host}`);
        }
        return originalConnect.apply(this, args);
    };

    if (globalThis.fetch) {
        const originalFetch = globalThis.fetch.bind(globalThis);
        globalThis.fetch = ((input: string | URL | Request, init?: RequestInit) => {
            const target = input instanceof Request ? input.url : input;
            assertLoopbackUrl(target);
            return originalFetch(input, init);
        }) as typeof globalThis.fetch;
    }
}
