import * as vscode from 'vscode';
import { AIRelaySidebarProvider } from './sidePanel';

let sidebarProvider: AIRelaySidebarProvider | undefined;

export function activate(context: vscode.ExtensionContext) {
    console.log('AI-Relay: Extension activating...');

    // Create sidebar provider
    sidebarProvider = new AIRelaySidebarProvider(context.extensionUri, context);

    // Register sidebar webview
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            'ai-relay.sidebar',
            sidebarProvider
        )
    );

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('ai-relay.status', () => {
            sidebarProvider?.runCommand('status');
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('ai-relay.capture', () => {
            sidebarProvider?.runCommand('capture');
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('ai-relay.handoff', () => {
            sidebarProvider?.showHandoffDialog();
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('ai-relay.start', () => {
            sidebarProvider?.runCommand('start');
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('ai-relay.continue', () => {
            sidebarProvider?.runCommand('continue');
        })
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('ai-relay.refresh', () => {
            sidebarProvider?.refresh();
        })
    );

    console.log('AI-Relay: Extension activated');
}

export function deactivate() {
    console.log('AI-Relay: Extension deactivated');
}
