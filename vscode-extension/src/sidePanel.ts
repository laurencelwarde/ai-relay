import * as vscode from 'vscode';
import * as path from 'path';
import { spawn } from 'child_process';

interface ProviderStatus {
    name: string;
    displayName: string;
    installed: boolean;
    status: string;
    used: number;
    limit: number | null;
    enabled: boolean;
}

interface SessionInfo {
    id: string;
    provider: string;
    created: string;
    tokens: number;
}

export class AIRelaySidebarProvider implements vscode.WebviewViewProvider {
    private _view?: vscode.WebviewView;
    private _extensionUri: vscode.Uri;
    private _providers: ProviderStatus[] = [];
    private _session: SessionInfo | null = null;
    private _isLoading: boolean = false;

    constructor(extensionUri: vscode.Uri) {
        this._extensionUri = extensionUri;
    }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ) {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri]
        };

        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        // Handle messages from webview
        webviewView.webview.onDidReceiveMessage(async (message) => {
            switch (message.command) {
                case 'refresh':
                    await this.refresh();
                    break;
                case 'toggleProvider':
                    this.toggleProvider(message.provider);
                    break;
                case 'startSession':
                    await this.runCommand('start', message.provider);
                    break;
                case 'continueSession':
                    await this.runCommand('continue');
                    break;
                case 'handoff':
                    await this.showHandoffDialog();
                    break;
                case 'capture':
                    await this.runCommand('capture', message.provider);
                    break;
                case 'setQuota':
                    await this.setQuota(message.provider, message.limit);
                    break;
            }
        });

        // Initial load
        this.refresh();
    }

    public async refresh() {
        if (this._isLoading) return;
        this._isLoading = true;

        try {
            const status = await this._runPythonCommand(['status', '--json']);
            if (status) {
                const parsed = JSON.parse(status);
                this._providers = parsed.providers || [];
                this._session = parsed.session || null;
            }
        } catch (error) {
            console.error('AI-Relay: Failed to get status', error);
        }

        this._isLoading = false;
        this._updateWebview();
    }

    public async runCommand(command: string, provider?: string) {
        const args = [command];
        if (provider) {
            args.push('--provider', provider);
        }

        try {
            const result = await this._runPythonCommand(args);
            if (result) {
                vscode.window.showInformationMessage(`AI-Relay: ${command} completed`);
            }
            await this.refresh();
        } catch (error) {
            vscode.window.showErrorMessage(`AI-Relay: ${command} failed - ${error}`);
        }
    }

    public async showHandoffDialog() {
        const availableProviders = this._providers
            .filter(p => p.installed && p.enabled && p.status === 'available')
            .map(p => ({ label: p.displayName, value: p.name }));

        if (availableProviders.length === 0) {
            vscode.window.showWarningMessage('AI-Relay: No providers available for handoff');
            return;
        }

        const selected = await vscode.window.showQuickPick(
            availableProviders.map(p => p.label),
            { placeHolder: 'Select provider to hand off to' }
        );

        if (selected) {
            const provider = availableProviders.find(p => p.label === selected);
            if (provider) {
                await this.runCommand('handoff', provider.value);
            }
        }
    }

    public toggleProvider(providerName: string) {
        const provider = this._providers.find(p => p.name === providerName);
        if (provider) {
            provider.enabled = !provider.enabled;
            this._updateWebview();
        }
    }

    public async setQuota(provider: string, limit: number) {
        try {
            await this._runPythonCommand(['config', 'set', provider, '--limit', limit.toString()]);
            await this.refresh();
        } catch (error) {
            vscode.window.showErrorMessage(`AI-Relay: Failed to set quota - ${error}`);
        }
    }

    private async _runPythonCommand(args: string[]): Promise<string> {
        return new Promise((resolve, reject) => {
            const config = vscode.workspace.getConfiguration('ai-relay');
            const pythonPath = config.get<string>('pythonPath') || 'python';
            let scriptPath = config.get<string>('scriptPath') || '';

            if (!scriptPath) {
                // Use bundled script
                scriptPath = path.join(this._extensionUri.fsPath, '..', 'ai_relay.py');
            }

            const workspaceFolders = vscode.workspace.workspaceFolders;
            const cwd = workspaceFolders ? workspaceFolders[0].uri.fsPath : process.cwd();

            const proc = spawn(pythonPath, [scriptPath, ...args], {
                cwd,
                env: { ...process.env }
            });

            let stdout = '';
            let stderr = '';

            proc.stdout.on('data', (data) => {
                stdout += data.toString();
            });

            proc.stderr.on('data', (data) => {
                stderr += data.toString();
            });

            proc.on('close', (code) => {
                if (code === 0) {
                    resolve(stdout);
                } else {
                    reject(new Error(stderr || `Exit code ${code}`));
                }
            });

            proc.on('error', (err) => {
                reject(err);
            });
        });
    }

    private _updateWebview() {
        if (!this._view) return;
        this._view.webview.postMessage({
            type: 'update',
            providers: this._providers,
            session: this._session,
            isLoading: this._isLoading
        });
    }

    private _getHtmlForWebview(webview: vscode.Webview): string {
        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI-Relay</title>
    <style>
        :root {
            --primary-color: #8B5CF6;
            --primary-hover: #7C3AED;
            --success-color: #10B981;
            --warning-color: #F59E0B;
            --error-color: #EF4444;
            --bg-secondary: var(--vscode-sideBar-background);
            --border-color: var(--vscode-panel-border);
        }

        body {
            font-family: var(--vscode-font-family);
            font-size: var(--vscode-font-size);
            color: var(--vscode-foreground);
            padding: 12px;
            margin: 0;
        }

        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border-color);
        }

        .header h2 {
            margin: 0;
            font-size: 14px;
            font-weight: 600;
        }

        .refresh-btn {
            background: none;
            border: none;
            cursor: pointer;
            color: var(--vscode-foreground);
            padding: 4px;
            opacity: 0.7;
        }

        .refresh-btn:hover {
            opacity: 1;
        }

        .section {
            margin-bottom: 20px;
        }

        .section-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--vscode-descriptionForeground);
            margin-bottom: 8px;
        }

        .provider-card {
            background: var(--vscode-input-background);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 10px;
            margin-bottom: 8px;
        }

        .provider-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .provider-info {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .provider-name {
            font-weight: 500;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }

        .status-available { background: var(--success-color); }
        .status-rate_limited { background: var(--warning-color); }
        .status-not_installed { background: var(--vscode-descriptionForeground); }
        .status-error { background: var(--error-color); }

        .toggle {
            position: relative;
            width: 36px;
            height: 20px;
        }

        .toggle input {
            opacity: 0;
            width: 0;
            height: 0;
        }

        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: var(--vscode-input-background);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            transition: 0.2s;
        }

        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 14px;
            width: 14px;
            left: 2px;
            bottom: 2px;
            background-color: var(--vscode-foreground);
            border-radius: 50%;
            transition: 0.2s;
        }

        .toggle input:checked + .toggle-slider {
            background-color: var(--primary-color);
            border-color: var(--primary-color);
        }

        .toggle input:checked + .toggle-slider:before {
            transform: translateX(16px);
            background-color: white;
        }

        .quota-bar {
            height: 4px;
            background: var(--vscode-progressBar-background);
            border-radius: 2px;
            margin-top: 8px;
            overflow: hidden;
        }

        .quota-fill {
            height: 100%;
            background: var(--primary-color);
            border-radius: 2px;
            transition: width 0.3s;
        }

        .quota-text {
            font-size: 10px;
            color: var(--vscode-descriptionForeground);
            margin-top: 4px;
        }

        .session-card {
            background: linear-gradient(135deg, var(--primary-color)20, var(--primary-color)10);
            border: 1px solid var(--primary-color)40;
            border-radius: 6px;
            padding: 12px;
        }

        .session-title {
            font-weight: 500;
            margin-bottom: 4px;
        }

        .session-meta {
            font-size: 11px;
            color: var(--vscode-descriptionForeground);
        }

        .button-group {
            display: flex;
            gap: 8px;
            margin-top: 12px;
        }

        .btn {
            flex: 1;
            padding: 8px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 500;
            transition: background 0.2s;
        }

        .btn-primary {
            background: var(--primary-color);
            color: white;
        }

        .btn-primary:hover {
            background: var(--primary-hover);
        }

        .btn-secondary {
            background: var(--vscode-button-secondaryBackground);
            color: var(--vscode-button-secondaryForeground);
        }

        .btn-secondary:hover {
            background: var(--vscode-button-secondaryHoverBackground);
        }

        .no-session {
            text-align: center;
            padding: 20px;
            color: var(--vscode-descriptionForeground);
        }

        .loading {
            text-align: center;
            padding: 20px;
            color: var(--vscode-descriptionForeground);
        }

        .spinner {
            display: inline-block;
            width: 16px;
            height: 16px;
            border: 2px solid var(--border-color);
            border-top-color: var(--primary-color);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-right: 8px;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="header">
        <h2>AI-Relay</h2>
        <button class="refresh-btn" onclick="refresh()" title="Refresh">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <path d="M13.451 5.609l-.579-.939-1.068.812-.076.094c-.335.415-.927 1.341-1.124 2.876l-.021.165.033.163.071.345c.013.062.027.126.038.189H5.309l1.647-1.647-.708-.707L3.126 9.12l3.122 3.122.708-.707L5.308 9.887h5.917l.076-.153c.036-.072.07-.144.1-.218.039-.093.074-.19.104-.29.014-.045.026-.091.037-.137.052-.21.085-.435.094-.673.012-.32-.018-.67-.096-1.03-.081-.379-.216-.768-.406-1.15-.088-.177-.188-.354-.296-.525-.064-.1-.132-.199-.201-.296-.087-.121-.178-.238-.273-.35a5.308 5.308 0 00-.259-.289l-.073-.076-.056-.054-.254.594z"/>
            </svg>
        </button>
    </div>

    <div id="content">
        <div class="loading">
            <span class="spinner"></span>
            Loading...
        </div>
    </div>

    <script>
        const vscode = acquireVsCodeApi();
        let state = {
            providers: [],
            session: null,
            isLoading: true
        };

        function refresh() {
            vscode.postMessage({ command: 'refresh' });
        }

        function toggleProvider(provider) {
            vscode.postMessage({ command: 'toggleProvider', provider });
        }

        function startSession(provider) {
            vscode.postMessage({ command: 'startSession', provider });
        }

        function continueSession() {
            vscode.postMessage({ command: 'continueSession' });
        }

        function handoff() {
            vscode.postMessage({ command: 'handoff' });
        }

        function render() {
            const content = document.getElementById('content');

            if (state.isLoading) {
                content.innerHTML = '<div class="loading"><span class="spinner"></span>Loading...</div>';
                return;
            }

            let html = '';

            // Session section
            html += '<div class="section">';
            html += '<div class="section-title">Current Session</div>';

            if (state.session) {
                html += '<div class="session-card">';
                html += '<div class="session-title">' + state.session.provider + '</div>';
                html += '<div class="session-meta">Session: ' + state.session.id.substring(0, 12) + '...</div>';
                html += '<div class="session-meta">Tokens: ' + (state.session.tokens || 0).toLocaleString() + '</div>';
                html += '<div class="button-group">';
                html += '<button class="btn btn-secondary" onclick="continueSession()">Continue</button>';
                html += '<button class="btn btn-primary" onclick="handoff()">Handoff</button>';
                html += '</div>';
                html += '</div>';
            } else {
                html += '<div class="no-session">';
                html += '<p>No active session</p>';
                html += '<button class="btn btn-primary" onclick="startSession()">Start Session</button>';
                html += '</div>';
            }

            html += '</div>';

            // Providers section
            html += '<div class="section">';
            html += '<div class="section-title">Providers</div>';

            for (const provider of state.providers) {
                html += '<div class="provider-card">';
                html += '<div class="provider-header">';
                html += '<div class="provider-info">';
                html += '<span class="status-dot status-' + provider.status + '"></span>';
                html += '<span class="provider-name">' + provider.displayName + '</span>';
                html += '</div>';
                html += '<label class="toggle">';
                html += '<input type="checkbox" ' + (provider.enabled ? 'checked' : '') + ' onchange="toggleProvider(\\''+provider.name+'\\')"/>';
                html += '<span class="toggle-slider"></span>';
                html += '</label>';
                html += '</div>';

                if (provider.limit) {
                    const percent = Math.min(100, (provider.used / provider.limit) * 100);
                    html += '<div class="quota-bar"><div class="quota-fill" style="width: ' + percent + '%"></div></div>';
                    html += '<div class="quota-text">' + provider.used + ' / ' + provider.limit + ' tokens</div>';
                }

                html += '</div>';
            }

            html += '</div>';

            content.innerHTML = html;
        }

        window.addEventListener('message', event => {
            const message = event.data;
            if (message.type === 'update') {
                state.providers = message.providers || [];
                state.session = message.session;
                state.isLoading = message.isLoading;
                render();
            }
        });

        // Initial render
        render();
    </script>
</body>
</html>`;
    }
}
