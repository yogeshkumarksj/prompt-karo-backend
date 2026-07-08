import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/prompt_model.dart';

class ApiService {
  // Chrome के लिए localhost (127.0.0.1) सही है। 
  // अगर आप Android Emulator इस्तेमाल कर रहे हैं तो इसे "http://10.0.2.2:8000" करें।
  final String baseUrl = "http://127.0.0.1:8000"; 

  Future<List<Prompt>> fetchPrompts() async {
    try {
      final response = await http.get(Uri.parse("$baseUrl/prompts/"));

      if (response.statusCode == 200) {
        List<dynamic> body = jsonDecode(response.body);
        
        return body.map((item) => Prompt(
          id: item['id'],
          title: item['title'],
          imageUrl: item['image_url'],
          // यहाँ हमने 'prompt_text' को मैप किया है जो Backend से आ रहा है
          promptText: item['prompt_text'] ?? "No prompt text available", 
          category: "AI Art", 
        )).toList();
      } else {
        throw Exception("Failed to load prompts: ${response.statusCode}");
      }
    } catch (e) {
      print("ApiService Error: $e");
      return []; 
    }
  }
}