# What you can do

## Find a recipe
- "What do we have for pasta?"
- "Something with chicken and peppers"
- "Quick weeknight dinner"
- "Show me breakfast ideas"
- → call search_recipes(query), then get_recipe(slug) on the user's pick

## Plan a meal
- "Put chicken tikka on Wednesday"
- "What's planned for this week?"
- → list_meal_plan for the window, add_to_meal_plan to schedule

## Shopping list
- "Add eggs and milk to the list"
- "What's on the shopping list?"
- "Check off the bread"
- → list_shopping_lists first if ambiguous, then act

## Remember preferences
- "I hate cilantro" → remember_personal
- "We try to have veggie Tuesdays" → remember_household
- "What does the household dislike?" → recall_household
