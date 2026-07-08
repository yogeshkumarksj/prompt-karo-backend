class Prompt {
  final int id;
  final String title;
  final String imageUrl;
  final String category;
  final String promptText; // यह बहुत ज़रूरी है

  Prompt({
    required this.id,
    required this.title,
    required this.imageUrl,
    required this.category,
    required this.promptText,
  });
}