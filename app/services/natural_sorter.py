import re
from typing import List, Any

class NaturalSorter:
    """
    Service untuk melakukan natural sorting pada filename.
    Memastikan urutan: 001, 002, 010 (bukan 001, 010, 002)
    
    Mendukung berbagai format:
    - image-001.jpg, image-002.jpg
    - snju-001.png, snju-002.png
    - 001.jpg, 002.jpg, 010.jpg
    """
    
    @staticmethod
    def extract_numbers(text: str) -> List:
        """
        Ekstrak angka dari string untuk sorting.
        Contoh: "image-001.jpg" -> ['image-', 1, '.jpg']
        """
        def convert(part):
            return int(part) if part.isdigit() else part.lower()
        
        return [convert(c) for c in re.split('([0-9]+)', text)]
    
    @staticmethod
    def natural_sort(items: List[str]) -> List[str]:
        """
        Sort list of strings secara natural.
        
        Args:
            items: List of filenames
            
        Returns:
            Sorted list
            
        Example:
            >>> NaturalSorter.natural_sort(['img-10.jpg', 'img-2.jpg', 'img-1.jpg'])
            ['img-1.jpg', 'img-2.jpg', 'img-10.jpg']
        """
        return sorted(items, key=NaturalSorter.extract_numbers)
    
    @staticmethod
    def natural_sort_dict(items: List[dict], key_field: str = 'name') -> List[dict]:
        """
        Sort list of dictionaries berdasarkan field tertentu.
        
        Args:
            items: List of dictionaries
            key_field: Field name yang akan dijadikan kunci sorting
            
        Returns:
            Sorted list of dictionaries
            
        Example:
            >>> files = [{'name': 'img-10.jpg'}, {'name': 'img-2.jpg'}]
            >>> NaturalSorter.natural_sort_dict(files, 'name')
            [{'name': 'img-2.jpg'}, {'name': 'img-10.jpg'}]
        """
        return sorted(items, key=lambda x: NaturalSorter.extract_numbers(x.get(key_field, '')))
    
    @staticmethod
    def natural_sort_objects(items: List[Any], attr_name: str = 'name') -> List[Any]:
        """
        Sort list of objects berdasarkan attribute tertentu.
        
        Args:
            items: List of objects
            attr_name: Attribute name yang akan dijadikan kunci sorting
            
        Returns:
            Sorted list of objects
        """
        return sorted(items, key=lambda x: NaturalSorter.extract_numbers(getattr(x, attr_name, '')))