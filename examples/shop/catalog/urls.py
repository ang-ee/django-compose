from django.apps import apps
from django.http import JsonResponse
from django.urls import path


def items(request):
    Item = apps.get_model("catalog", "Item")
    return JsonResponse({"items": list(Item.objects.values("title", "rating"))})


urlpatterns = [path("items/", items, name="items")]
