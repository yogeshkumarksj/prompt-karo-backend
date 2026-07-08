// lib/screens/home_screen.dart
import 'package:flutter/material.dart';
import 'package:flutter_staggered_grid_view/flutter_staggered_grid_view.dart';
import '../models/prompt_model.dart';
import '../widgets/prompt_card.dart';
import '../services/api_service.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final ApiService apiService = ApiService();
  late Future<List<Prompt>> futurePrompts;

  @override
  void initState() {
    super.initState();
    futurePrompts = apiService.fetchPrompts(); // ऐप खुलते ही डेटा मांगो
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text("PromptVerse", style: TextStyle(fontWeight: FontWeight.bold)),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: () {
              setState(() {
                futurePrompts = apiService.fetchPrompts(); // रिफ्रेश बटन
              });
            },
          )
        ],
      ),
      body: FutureBuilder<List<Prompt>>(
        future: futurePrompts,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator()); // लोडिंग स्पिनर
          } else if (snapshot.hasError || snapshot.data == null || snapshot.data!.isEmpty) {
            return const Center(child: Text("No Prompts found. Start your Backend!"));
          }

          final prompts = snapshot.data!;
          return MasonryGridView.count(
            padding: const EdgeInsets.all(10),
            crossAxisCount: 2,
            mainAxisSpacing: 10,
            crossAxisSpacing: 10,
            itemCount: prompts.length,
            itemBuilder: (context, index) {
              return PromptCard(prompt: prompts[index]);
            },
          );
        },
      ),
    );
  }
}