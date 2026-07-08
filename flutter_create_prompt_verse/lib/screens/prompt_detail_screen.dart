import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:share_plus/share_plus.dart';
import '../models/prompt_model.dart';

class PromptDetailScreen extends StatelessWidget {
  final Prompt prompt;
  const PromptDetailScreen({super.key, required this.prompt});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text("Prompt Details")),
      body: SingleChildScrollView(
        child: Column(
          children: [
            Image.network(prompt.imageUrl, width: double.infinity, fit: BoxFit.cover),
            Padding(
              padding: const EdgeInsets.all(16.0),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(prompt.title, style: const TextStyle(fontSize: 24, fontWeight: FontWeight.bold)),
                  const SizedBox(height: 20),
                  const Text("AI Prompt:", style: TextStyle(fontSize: 18, color: Colors.grey)),
                  const SizedBox(height: 10),
                  Container(
                    padding: const EdgeInsets.all(15),
                    decoration: BoxDecoration(
                      color: Colors.white.withOpacity(0.05),
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Text(prompt.promptText, style: const TextStyle(fontSize: 16, height: 1.5)),
                  ),
                  const SizedBox(height: 30),
                  Row(
                    children: [
                      Expanded(
                        child: ElevatedButton.icon(
                          onPressed: () {
                            Clipboard.setData(ClipboardData(text: prompt.promptText));
                            ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text("Copied!")));
                          },
                          icon: const Icon(Icons.copy),
                          label: const Text("Copy Prompt"),
                          style: ElevatedButton.styleFrom(backgroundColor: const Color(0xFF8B5CF6), padding: const EdgeInsets.all(15)),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}