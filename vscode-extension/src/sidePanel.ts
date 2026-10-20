import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
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
    public static readonly viewType = 'ai-relay.sidebar';
    private _view?: vscode.WebviewView;
    private _extensionUri: vscode.Uri;
    private _context: vscode.ExtensionContext;
    private _providers: ProviderStatus[] = [];
    private _session: SessionInfo | null = null;
    private _isLoading: boolean = false;
    private _scriptFound: boolean = false;
    private _lastError: string = '';

    constructor(extensionUri: vscode.Uri, context: vscode.ExtensionContext) {
        this._extensionUri = extensionUri;
        this._context = context;
    }

    private _getToggleStates(): Record<string, boolean> {
        return this._context.globalState.get('ai-relay.toggleStates', {
            claude: true,
            codex: true,
            grok: true,
            gemini: false,
            ollama: false
        });
    }

    private _saveToggleStates(toggles: Record<string, boolean>) {
        this._context.globalState.update('ai-relay.toggleStates', toggles);
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
                case 'webviewReady':
                    await this.refresh();
                    break;
                case 'refresh':
                    await this.refresh();
                    break;
                case 'toggleProvider':
                    this._toggleProvider(message.provider, message.enabled);
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
                    await this.runCommand('capture');
                    break;
                case 'openSettings':
                    vscode.commands.executeCommand('workbench.action.openSettings', 'ai-relay');
                    break;
                case 'setQuota':
                    await this.setQuota(message.provider, message.limit);
                    break;
            }
        });
    }

    private _toggleProvider(providerName: string, enabled: boolean) {
        const toggles = this._getToggleStates();
        toggles[providerName] = enabled;
        this._saveToggleStates(toggles);

        // Update local state
        const provider = this._providers.find(p => p.name === providerName);
        if (provider) {
            provider.enabled = enabled;
        }
        this._updateWebview();
    }

    public async refresh() {
        if (this._isLoading) return;
        this._isLoading = true;
        this._lastError = '';

        try {
            const status = await this._runPythonCommand(['status', '--json']);
            if (status) {
                const parsed = JSON.parse(status);
                this._providers = parsed.providers || [];
                this._session = parsed.session || null;
                this._scriptFound = true;

                // Apply saved toggle states
                const toggles = this._getToggleStates();
                this._providers.forEach(p => {
                    if (toggles.hasOwnProperty(p.name)) {
                        p.enabled = toggles[p.name];
                    }
                });
            }
        } catch (error: any) {
            console.error('AI-Relay: Failed to get status', error);
            this._lastError = error.message || 'Unknown error';
            this._scriptFound = false;

            // Provide default providers when script not found
            if (this._providers.length === 0) {
                const toggles = this._getToggleStates();
                this._providers = [
                    { name: 'claude', displayName: 'Claude Code', installed: false, status: 'unknown', used: 0, limit: null, enabled: toggles.claude },
                    { name: 'codex', displayName: 'Codex CLI', installed: false, status: 'unknown', used: 0, limit: null, enabled: toggles.codex },
                    { name: 'grok', displayName: 'Grok CLI', installed: false, status: 'unknown', used: 0, limit: null, enabled: toggles.grok },
                    { name: 'gemini', displayName: 'Gemini CLI', installed: false, status: 'unknown', used: 0, limit: null, enabled: toggles.gemini },
                    { name: 'ollama', displayName: 'Ollama (Local)', installed: false, status: 'unknown', used: 0, limit: null, enabled: toggles.ollama },
                ];
            }
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

    public async setQuota(provider: string, limit: number) {
        try {
            await this._runPythonCommand(['config', 'set', provider, '--limit', limit.toString()]);
            await this.refresh();
        } catch (error) {
            vscode.window.showErrorMessage(`AI-Relay: Failed to set quota - ${error}`);
        }
    }

    private _findScriptPath(): string | null {
        const config = vscode.workspace.getConfiguration('ai-relay');
        const configPath = config.get<string>('scriptPath');

        // 1. Check configured path
        if (configPath && fs.existsSync(configPath)) {
            return configPath;
        }

        // 2. Check workspace folders
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (workspaceFolders) {
            for (const folder of workspaceFolders) {
                const wsPath = path.join(folder.uri.fsPath, 'ai_relay.py');
                if (fs.existsSync(wsPath)) {
                    return wsPath;
                }
            }
        }

        // 3. Check common locations
        const commonPaths = [
            'C:\\AI-RELAY\\ai_relay.py',
            path.join(this._extensionUri.fsPath, '..', 'ai_relay.py'),
            path.join(process.env.USERPROFILE || '', 'AI-RELAY', 'ai_relay.py'),
        ];

        for (const p of commonPaths) {
            if (fs.existsSync(p)) {
                return p;
            }
        }

        return null;
    }

    private async _runPythonCommand(args: string[]): Promise<string> {
        return new Promise((resolve, reject) => {
            const config = vscode.workspace.getConfiguration('ai-relay');
            const pythonPath = config.get<string>('pythonPath') || 'python';
            const scriptPath = this._findScriptPath();

            if (!scriptPath) {
                reject(new Error('ai_relay.py not found. Set ai-relay.scriptPath in settings.'));
                return;
            }

            const workspaceFolders = vscode.workspace.workspaceFolders;
            const cwd = workspaceFolders ? workspaceFolders[0].uri.fsPath : process.cwd();

            const proc = spawn(pythonPath, [scriptPath, ...args], {
                cwd,
                env: { ...process.env },
                shell: true
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
            isLoading: this._isLoading,
            scriptFound: this._scriptFound,
            lastError: this._lastError
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
            --primary-dim: rgba(139, 92, 246, 0.2);
            --success-color: #10B981;
            --warning-color: #F59E0B;
            --error-color: #EF4444;
            --bg-secondary: var(--vscode-sideBar-background);
            --border-color: var(--vscode-panel-border);
        }

        * { box-sizing: border-box; }

        body {
            font-family: var(--vscode-font-family);
            font-size: var(--vscode-font-size);
            color: var(--vscode-foreground);
            padding: 0;
            margin: 0;
        }

        .container {
            padding: 12px;
        }

        /* Header */
        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 12px;
            background: var(--primary-dim);
            border-bottom: 1px solid var(--border-color);
        }

        .header-title {
            display: flex;
            align-items: center;
            gap: 8px;
            font-weight: 600;
            font-size: 13px;
        }

        .header-actions {
            display: flex;
            gap: 4px;
        }

        .icon-btn {
            background: none;
            border: none;
            cursor: pointer;
            color: var(--vscode-foreground);
            padding: 4px;
            border-radius: 4px;
            opacity: 0.7;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .icon-btn:hover {
            opacity: 1;
            background: var(--vscode-toolbar-hoverBackground);
        }

        /* Sections */
        .section {
            margin-bottom: 16px;
        }

        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 6px 0;
            cursor: pointer;
            user-select: none;
        }

        .section-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--vscode-descriptionForeground);
            font-weight: 600;
        }

        .section-content {
            margin-top: 8px;
        }

        /* Provider Cards */
        .provider-card {
            background: var(--vscode-input-background);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 10px 12px;
            margin-bottom: 8px;
            transition: border-color 0.2s;
        }

        .provider-card:hover {
            border-color: var(--primary-color);
        }

        .provider-card.disabled {
            opacity: 0.5;
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
            font-size: 12px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .status-available { background: var(--success-color); }
        .status-rate_limited { background: var(--warning-color); }
        .status-not_installed { background: var(--vscode-descriptionForeground); }
        .status-unknown { background: var(--vscode-descriptionForeground); }
        .status-error { background: var(--error-color); }

        .badge {
            font-size: 9px;
            padding: 2px 6px;
            border-radius: 10px;
            text-transform: uppercase;
            font-weight: 600;
        }

        .badge-installed { background: var(--success-color); color: white; }
        .badge-missing { background: var(--error-color); color: white; }
        .badge-cli { background: var(--primary-color); color: white; }

        /* Toggle Switch */
        .toggle {
            position: relative;
            width: 32px;
            height: 18px;
            flex-shrink: 0;
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
            border-radius: 9px;
            transition: 0.2s;
        }

        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 12px;
            width: 12px;
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
            transform: translateX(14px);
            background-color: white;
        }

        /* Quota Bar */
        .quota-row {
            margin-top: 8px;
            padding-top: 8px;
            border-top: 1px solid var(--border-color);
        }

        .quota-bar {
            height: 4px;
            background: var(--vscode-progressBar-background);
            border-radius: 2px;
            overflow: hidden;
        }

        .quota-fill {
            height: 100%;
            background: var(--primary-color);
            border-radius: 2px;
            transition: width 0.3s;
        }

        .quota-fill.warning { background: var(--warning-color); }
        .quota-fill.danger { background: var(--error-color); }

        .quota-text {
            font-size: 10px;
            color: var(--vscode-descriptionForeground);
            margin-top: 4px;
            display: flex;
            justify-content: space-between;
        }

        /* Session Card */
        .session-card {
            background: linear-gradient(135deg, rgba(139, 92, 246, 0.15), rgba(139, 92, 246, 0.05));
            border: 1px solid rgba(139, 92, 246, 0.3);
            border-radius: 8px;
            padding: 12px;
        }

        .session-provider {
            font-weight: 600;
            font-size: 14px;
            margin-bottom: 4px;
        }

        .session-meta {
            font-size: 11px;
            color: var(--vscode-descriptionForeground);
            margin-bottom: 2px;
        }

        .session-actions {
            display: flex;
            gap: 8px;
            margin-top: 12px;
        }

        /* Buttons */
        .btn {
            flex: 1;
            padding: 8px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 500;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
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

        .btn-sm {
            padding: 6px 10px;
            font-size: 11px;
        }

        /* No Session */
        .no-session {
            text-align: center;
            padding: 24px 12px;
            background: var(--vscode-input-background);
            border-radius: 8px;
            border: 1px dashed var(--border-color);
        }

        .no-session p {
            margin: 0 0 12px 0;
            color: var(--vscode-descriptionForeground);
        }

        /* Error/Warning */
        .error-box {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid var(--error-color);
            border-radius: 6px;
            padding: 10px 12px;
            margin-bottom: 12px;
            font-size: 11px;
        }

        .error-box code {
            background: rgba(0,0,0,0.2);
            padding: 2px 4px;
            border-radius: 3px;
            font-size: 10px;
        }

        /* Loading */
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

        /* Settings Link */
        .settings-link {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 8px 12px;
            background: var(--vscode-input-background);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            color: var(--vscode-foreground);
            transition: border-color 0.2s;
        }

        .settings-link:hover {
            border-color: var(--primary-color);
        }

        /* Provider status row */
        .provider-status {
            display: flex;
            align-items: center;
            gap: 6px;
            margin-top: 4px;
            font-size: 10px;
            color: var(--vscode-descriptionForeground);
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-title">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="5" cy="12" r="3"/>
                <circle cx="19" cy="12" r="3"/>
                <path d="M8 12h8"/>
                <path d="M14 8l4 4-4 4"/>
            </svg>
            AI-Relay
        </div>
        <div class="header-actions">
            <button class="icon-btn" onclick="openSettings()" title="Settings">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M9.1 4.4L8.6 2H7.4l-.5 2.4-.7.3-2-1.3-.9.8 1.3 2-.2.7-2.4.5v1.2l2.4.5.3.8-1.3 2 .8.8 2-1.3.8.3.4 2.3h1.2l.5-2.4.8-.3 2 1.3.8-.8-1.3-2 .3-.8 2.3-.4V7.4l-2.4-.5-.3-.8 1.3-2-.8-.8-2 1.3-.7-.2zM8 10a2 2 0 1 1 0-4 2 2 0 0 1 0 4z"/>
                </svg>
            </button>
            <button class="icon-btn" onclick="refresh()" title="Refresh">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M4.5 3.5A5 5 0 0 1 13 8h-1.5l2 3 2-3H14a6 6 0 0 0-10.6-3.9l1.1.4zM11.5 12.5A5 5 0 0 1 3 8h1.5l-2-3-2 3H2a6 6 0 0 0 10.6 3.9l-1.1-.4z"/>
                </svg>
            </button>
        </div>
    </div>

    <div class="container">
        <div id="content">
            <div class="loading">
                <span class="spinner"></span>
                Loading...
            </div>
        </div>
    </div>

    <script>
        const vscode = acquireVsCodeApi();
        let state = {
            providers: [],
            session: null,
            isLoading: true,
            scriptFound: true,
            lastError: ''
        };

        function refresh() {
            vscode.postMessage({ command: 'refresh' });
        }

        function openSettings() {
            vscode.postMessage({ command: 'openSettings' });
        }

        function toggleProvider(provider, checkbox) {
            vscode.postMessage({
                command: 'toggleProvider',
                provider: provider,
                enabled: checkbox.checked
            });
        }

        function startSession(provider) {
            vscode.postMessage({ command: 'startSession', provider: provider || undefined });
        }

        function continueSession() {
            vscode.postMessage({ command: 'continueSession' });
        }

        function handoff() {
            vscode.postMessage({ command: 'handoff' });
        }

        function capture() {
            vscode.postMessage({ command: 'capture' });
        }

        function getStatusClass(status) {
            return 'status-' + (status || 'unknown');
        }

        function getStatusText(status, installed) {
            if (!installed) return 'Not installed';
            switch(status) {
                case 'available': return 'Ready';
                case 'rate_limited': return 'Rate limited';
                case 'error': return 'Error';
                default: return status || 'Unknown';
            }
        }

        function render() {
            const content = document.getElementById('content');

            if (state.isLoading) {
                content.innerHTML = '<div class="loading"><span class="spinner"></span>Loading...</div>';
                return;
            }

            let html = '';

            // Error message if script not found
            if (!state.scriptFound && state.lastError) {
                html += '<div class="error-box">';
                html += '<strong>Setup Required</strong><br>';
                html += 'Could not find <code>ai_relay.py</code>.<br>';
                html += 'Click Settings (⚙) to configure the script path.';
                html += '</div>';
            }

            // Session section
            html += '<div class="section">';
            html += '<div class="section-title">Current Session</div>';
            html += '<div class="section-content">';

            if (state.session) {
                html += '<div class="session-card">';
                html += '<div class="session-provider">' + (state.session.provider || 'Unknown') + '</div>';
                html += '<div class="session-meta">Session: ' + (state.session.id || '').substring(0, 12) + '...</div>';
                html += '<div class="session-meta">Tokens: ' + (state.session.tokens || 0).toLocaleString() + '</div>';
                html += '<div class="session-actions">';
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

            html += '</div></div>';

            // Providers section
            html += '<div class="section">';
            html += '<div class="section-title">Providers</div>';
            html += '<div class="section-content">';

            if (state.providers.length === 0) {
                html += '<div class="no-session"><p>No providers configured</p></div>';
            } else {
                for (const provider of state.providers) {
                    const cardClass = provider.enabled ? '' : ' disabled';
                    html += '<div class="provider-card' + cardClass + '">';
                    html += '<div class="provider-header">';
                    html += '<div class="provider-info">';
                    html += '<span class="status-dot ' + getStatusClass(provider.status) + '"></span>';
                    html += '<span class="provider-name">' + provider.displayName + '</span>';
                    if (provider.installed) {
                        html += '<span class="badge badge-cli">CLI</span>';
                    }
                    html += '</div>';
                    html += '<label class="toggle">';
                    html += '<input type="checkbox" ' + (provider.enabled ? 'checked' : '') + ' onchange="toggleProvider(\\'' + provider.name + '\\', this)"/>';
                    html += '<span class="toggle-slider"></span>';
                    html += '</label>';
                    html += '</div>';

                    // Status text
                    html += '<div class="provider-status">';
                    html += getStatusText(provider.status, provider.installed);
                    html += '</div>';

                    // Quota bar (if limit set)
                    if (provider.limit && provider.limit > 0) {
                        const percent = Math.min(100, (provider.used / provider.limit) * 100);
                        const fillClass = percent > 90 ? ' danger' : (percent > 70 ? ' warning' : '');
                        html += '<div class="quota-row">';
                        html += '<div class="quota-bar"><div class="quota-fill' + fillClass + '" style="width: ' + percent + '%"></div></div>';
                        html += '<div class="quota-text"><span>' + provider.used + ' / ' + provider.limit + '</span><span>' + Math.round(percent) + '%</span></div>';
                        html += '</div>';
                    }

                    html += '</div>';
                }
            }

            html += '</div></div>';

            // Quick actions
            html += '<div class="section">';
            html += '<div class="section-title">Quick Actions</div>';
            html += '<div class="section-content">';
            html += '<button class="btn btn-secondary btn-sm" onclick="capture()" style="width:100%; margin-bottom:8px;">Capture Context</button>';
            html += '<div class="settings-link" onclick="openSettings()">';
            html += '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M9.1 4.4L8.6 2H7.4l-.5 2.4-.7.3-2-1.3-.9.8 1.3 2-.2.7-2.4.5v1.2l2.4.5.3.8-1.3 2 .8.8 2-1.3.8.3.4 2.3h1.2l.5-2.4.8-.3 2 1.3.8-.8-1.3-2 .3-.8 2.3-.4V7.4l-2.4-.5-.3-.8 1.3-2-.8-.8-2 1.3-.7-.2zM8 10a2 2 0 1 1 0-4 2 2 0 0 1 0 4z"/></svg>';
            html += 'Open Extension Settings';
            html += '</div>';
            html += '</div></div>';

            content.innerHTML = html;
        }

        window.addEventListener('message', event => {
            const message = event.data;
            if (message.type === 'update') {
                state.providers = message.providers || [];
                state.session = message.session;
                state.isLoading = message.isLoading;
                state.scriptFound = message.scriptFound;
                state.lastError = message.lastError || '';
                render();
            }
        });

        // Tell extension we're ready
        vscode.postMessage({ command: 'webviewReady' });
    </script>
</body>
</html>`;
    }
}
